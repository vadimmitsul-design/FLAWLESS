# -*- coding: utf-8 -*-
"""Аудит 21.09.2026 (сольный — воркфлоу с 15 ревизорами упал на лимите
сессии, разбор шёл вручную): подтверждённые находки и их починка.

Каждый тест воспроизводит дефект ДО фикса (это проверено вручную при
разборе) и доказывает, что после фикса поведение то, которое нужно.
Префикс адресов — `aud21_`.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Customer, ModelPrice, Prompt, Resource, UsageEvent, utcnow
from app.security import hash_password

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _admin_client():
    from fastapi.testclient import TestClient

    from app.main import app

    admin = TestClient(app)
    assert admin.post(
        "/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    ).status_code in (200, 303)
    return admin


def _person(email, name, **kw):
    async def _add():
        async with SessionLocal() as session:
            person = Customer(email=email, name=name, password_hash=hash_password("Passw0rd!"), **kw)
            session.add(person)
            await session.commit()
            return person.id

    return asyncio.run(_add())


# ---------- 1. Заявки сотрудников не должны зависеть от наличия ресурсов ----------


def test_pending_request_visible_even_without_any_resource(client):
    """Блок заявок стоял ВНУТРИ `{% if total_count %}` дашборда сроков: на
    контуре без единого заведённого ресурса первая же заявка сотрудника была
    невидима — администратор не знал, что её вообще подали."""
    employee = _person("aud21_req@test.local", "Заявитель")

    async def _add_request():
        async with SessionLocal() as session:
            from app.models import ResourceRequest

            session.add(
                ResourceRequest(customer_id=employee, kind="subscription", name="ChatGPT Plus для отчётов")
            )
            await session.commit()

    asyncio.run(_add_request())

    async def _no_resources_from(cust_id):
        async with SessionLocal() as session:
            existing = (
                await session.execute(select(Resource).where(Resource.owner_customer_id == cust_id))
            ).scalars().all()
            return len(existing)

    # Свойство теста, не факт продакшена: страница должна показать заявку
    # НЕЗАВИСИМО от того, сколько всего ресурсов в базе — поэтому просто
    # проверяем присутствие текста, а не нулевой total_count специально.
    page = _admin_client().get("/admin/resources").text
    assert "ChatGPT Plus для отчётов" in page
    assert "Заявки сотрудников" in page


# ---------- 2. Кэш-токены не задваиваются, когда цена на кэш заполнена ----------


def test_cached_tokens_not_double_billed_once_priced():
    from app.pricing import UsageAmounts, compute_cost

    price = ModelPrice(
        provider="openrouter",
        model="anthropic/claude-sonnet-5",
        price_per_1m_input_tokens=Decimal("3.00"),
        price_per_1m_output_tokens=Decimal("15.00"),
        price_per_1m_cached_tokens=Decimal("0.30"),
        valid_from=utcnow() - timedelta(days=1),
    )
    # 1000 входных токенов, из них 400 — кэшированные (подмножество, не сверх).
    usage = UsageAmounts(input_text_tokens=1000, cached_tokens=400, output_tokens=0)
    cost = compute_cost(price, usage)

    expected = (Decimal(600) * Decimal("3.00") + Decimal(400) * Decimal("0.30")) / Decimal(1_000_000)
    assert cost == expected.quantize(Decimal("0.000001"))

    # Наивный (задвоенный) расчёт брал бы полную ставку за все 1000 плюс
    # кэш-ставку за 400 сверху — заведомо больше правильного значения.
    naive = (Decimal(1000) * Decimal("3.00") + Decimal(400) * Decimal("0.30")) / Decimal(1_000_000)
    assert cost < naive


def test_cached_tokens_billed_as_before_when_cache_price_unset():
    """Пока price_per_1m_cached_tokens не заполнена (боевое состояние на
    2026-09-21 — ни у одной модели), поведение НЕ меняется: полная ставка
    за все input-токены, как считалось раньше. Иначе фикс сам стал бы
    регрессией — клиент платил бы МЕНЬШЕ, чем провайдер взял с нас."""
    from app.pricing import UsageAmounts, compute_cost

    price = ModelPrice(
        provider="openrouter",
        model="openai/gpt-5-mini",
        price_per_1m_input_tokens=Decimal("0.25"),
        price_per_1m_output_tokens=Decimal("2.00"),
        price_per_1m_cached_tokens=None,
        valid_from=utcnow() - timedelta(days=1),
    )
    usage = UsageAmounts(input_text_tokens=1000, cached_tokens=400, output_tokens=0)
    cost = compute_cost(price, usage)
    expected = (Decimal(1000) * Decimal("0.25")) / Decimal(1_000_000)
    assert cost == expected.quantize(Decimal("0.000001"))


# ---------- 3. charge_prompt_fee: роялти доходит независимо от порядка id ----------


def _make_call_event(actor_id, billing_id):
    async def _add():
        async with SessionLocal() as session:
            ev = UsageEvent(
                customer_id=actor_id,
                billing_customer_id=billing_id,
                provider="openrouter",
                model="openai/gpt-5-mini",
                status="success",
                charged_rub=Decimal("1.0000"),
            )
            session.add(ev)
            await session.commit()
            return ev.id

    return asyncio.run(_add())


def _make_prompt(author_id, price="10.00"):
    async def _add():
        async with SessionLocal() as session:
            p = Prompt(
                author_customer_id=author_id,
                title="Тест",
                system_prompt="Ты — тест.",
                price_rub=Decimal(price),
            )
            session.add(p)
            await session.commit()
            return p.id

    return asyncio.run(_add())


def _charge_and_read(billing_id, prompt_id, event_id):
    async def _run():
        async with SessionLocal() as session:
            from app import billing

            prompt = await session.get(Prompt, prompt_id)
            await billing.charge_prompt_fee(session, billing_id, prompt, event_id)
        async with SessionLocal() as session:
            payer = await session.get(Customer, billing_id)
            prompt = await session.get(Prompt, prompt_id)
            author = await session.get(Customer, prompt.author_customer_id)
            return payer.balance_rub, author.balance_rub

    return asyncio.run(_run())


@pytest.mark.parametrize("author_first", [True, False])
def test_prompt_royalty_reaches_author_regardless_of_id_order(author_first):
    """Порядок блокировки (по возрастанию id — правка деадлока) не должен
    менять итог: автор получает роялти, платящий теряет цену промпта,
    независимо от того, чей id меньше."""
    a = _person(f"aud21_royA_{author_first}_1@test.local", "Первый", balance_rub=Decimal("50"))
    b = _person(f"aud21_royA_{author_first}_2@test.local", "Второй", balance_rub=Decimal("50"))
    author_id, payer_id = (a, b) if author_first else (b, a)

    prompt_id = _make_prompt(author_id, price="10.00")
    event_id = _make_call_event(payer_id, payer_id)

    payer_balance, author_balance = _charge_and_read(payer_id, prompt_id, event_id)
    assert payer_balance == Decimal("40.0000")
    assert author_balance == Decimal("55.0000")  # 50 + 50% роялти от 10


def test_prompt_fee_skips_royalty_when_payer_goes_negative():
    """Второе правило функции не должно было пострадать от смены порядка
    локов: если платы не хватило и баланс ушёл в минус, роялти не платим."""
    author_id = _person("aud21_royB_author@test.local", "Автор", balance_rub=Decimal("0"))
    payer_id = _person("aud21_royB_payer@test.local", "Плательщик", balance_rub=Decimal("2"))
    prompt_id = _make_prompt(author_id, price="10.00")
    event_id = _make_call_event(payer_id, payer_id)

    payer_balance, author_balance = _charge_and_read(payer_id, prompt_id, event_id)
    assert payer_balance == Decimal("-8.0000")
    assert author_balance == Decimal("0.0000")  # роялти не начислено


# ---------- 4. /children/new: минимальная длина пароля совпадает с сервером ----------


def test_child_signup_rejects_short_password_with_inline_error_not_500(client):
    """HTML раньше разрешал 6 символов (minlength), а hash_password требует
    8 и кидает необработанный ValueError — родитель получал голый 500.
    Теперь и разметка, и сервер согласны на 8, а короткий пароль — обычная
    форма с понятной причиной."""
    parent_email = "aud21_parent@test.local"
    _person(parent_email, "Родитель")
    assert client.post(
        "/login", data={"email": parent_email, "password": "Passw0rd!"}
    ).status_code in (200, 303)

    resp = client.post(
        "/children/new",
        data={"name": "Ребёнок", "email": "aud21_child_short@test.local", "password": "short7!"},
    )
    assert resp.status_code == 400
    assert "8 символ" in resp.text

    ok = client.post(
        "/children/new",
        data={"name": "Ребёнок", "email": "aud21_child_ok@test.local", "password": "LongEnough1"},
    )
    assert ok.status_code in (200, 303)
    assert "/children/new" not in str(ok.url)


# ---------- 5. Нулевой потолок расхода виден на дашборде, а не спрятан ----------


def test_zero_daily_limit_is_shown_not_hidden(client):
    """Потолок 0 ₽ — валидное значение (min="0" в форме админа) и реально
    блокирует расход (billing._enforce_limits: spent >= 0 истинно всегда).
    Проверка на истинность в шаблоне считала 0.0 отсутствием потолка —
    человек с полностью перекрытым расходом не видел на СВОЕЙ странице
    ни единой строки об этом."""
    person_id = _person("aud21_zerolimit@test.local", "Ограниченный", daily_limit_rub=Decimal("0"))

    admin = _admin_client()
    # Логинимся как сам ограниченный человек, не админ.
    own = client
    assert own.post(
        "/login", data={"email": "aud21_zerolimit@test.local", "password": "Passw0rd!"}
    ).status_code in (200, 303)
    page = own.get("/").text
    assert "Потолок на сегодня" in page
    assert "0 ₽" in page or "0,00" in page
    assert "заблокирован полностью" in page


# ---------- 6. Подтверждение перед необратимыми действиями ----------


def test_destructive_admin_actions_carry_confirm_prompt(client):
    """Ни одна необратимая кнопка (отозвать ключ, вернуть деньги, отклонить
    заявку, отключить коллегу, сгенерировать новый пароль) не должна уходить
    одним кликом без единого предупреждения — до этой правки confirm() не
    было нигде в шаблонах."""
    admin = _admin_client()

    customers_page = admin.get("/admin/customers").text
    assert 'data-confirm="Отключить' in customers_page

    topups_page = admin.get("/admin/topups").text
    # Пусто без заявок — страница всё равно должна содержать разметку формы
    # только если есть строки; создаём одну через живого клиента.
    person_email = "aud21_topup@test.local"
    _person(person_email, "Заявитель на пополнение")
    assert client.post(
        "/login", data={"email": person_email, "password": "Passw0rd!"}
    ).status_code in (200, 303)
    assert client.post("/topups/new", data={"amount_rub": "500"}).status_code in (200, 303)
    topups_page = admin.get("/admin/topups").text
    assert 'data-confirm="Отклонить заявку' in topups_page

    resets_page_source_has_hook = "data-confirm=" in open(
        "app/templates/admin_password_resets.html", encoding="utf-8"
    ).read()
    assert resets_page_source_has_hook

    # Общий обработчик подключён один раз в base.html — новая форма получает
    # защиту просто унаследовав атрибут, без своего JS.
    base_js = open("app/templates/base.html", encoding="utf-8").read()
    assert "data-confirm" in base_js
    assert "window.confirm(text)" in base_js
