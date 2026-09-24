"""Находки аудита 2026-09-13, партия 1: деньги.

Общее у всех четырёх — вызов проходит, поставщику платим мы, а с клиента не
списывается ничего. Отдаётся при этом честный 200, поэтому ни один тест на
коды ответа их не видел.

Префикс адресов — `audit_`: тестовая база одна на весь прогон, и совпадение
email с другим файлом роняет регистрацию 409-м (в проекте так уже обжигались).
"""

# -*- coding: utf-8 -*-
import asyncio
import math
import pathlib
import re
from datetime import UTC
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.chat import _allowed_extra_params
from app.api.validation import money_field
from app.core.config import settings
from app.core.formatting import csv_cell
from app.db import SessionLocal
from app.db.models import Customer, TopupRequest, UsageEvent, WalletLedger, utcnow
from app.integrations import llm
from app.services import billing, pricing
from app.services.reporting import USAGE_PAGE_LIMIT


def _plain(html: str) -> str:
    """Суммы выводятся по-русски: неразрывный пробел между тысячами и
    сущность &nbsp; перед знаком рубля. Для проверок это шум."""
    return html.replace("&nbsp;", " ").replace("\u00a0", " ")


ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Audit Tester", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _issue_api_key(client):
    r = client.post("/api-key/regenerate")
    assert r.status_code == 200
    m = re.search(r"nh_[A-Za-z0-9_-]+", r.text)
    assert m
    return m.group(0)


def _admin_client():
    from fastapi.testclient import TestClient

    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def _topup(client, admin, amount="50"):
    client.post("/topups/new", data={"amount_rub": amount})
    m = re.search(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    assert m
    assert admin.post(f"/admin/topups/{m.group(1)}/confirm").status_code in (200, 303)
    return int(m.group(1))


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _last_event(customer_id):
    async def _get():
        async with SessionLocal() as session:
            return (
                (
                    await session.execute(
                        select(UsageEvent)
                        .where(UsageEvent.billing_customer_id == customer_id)
                        .order_by(UsageEvent.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )

    return asyncio.run(_get())


# ---------- 1. клиент не может отключить себе биллинг стрима ----------


def _stream_without_usage():
    """Поток, в котором провайдер НЕ прислал финальный usage-чанк — ровно то,
    что происходит при include_usage=false. Возвращается четвёркой
    (алиас, провайдер, модель, генератор), как настоящий
    llm.chat_completion_with_fallback."""

    async def _gen():
        for piece in ("Full ", "model ", "answer"):
            yield {
                "id": "chatcmpl-audit",
                "choices": [{"index": 0, "delta": {"content": piece}}],
            }

    return "gpt-5-mini", "openrouter", "openai/gpt-5-mini", _gen()


def test_client_cannot_switch_stream_billing_off(client, monkeypatch):
    """stream_options.include_usage=false отключал финальный usage-чанк, без
    него цена не считалась и вызов проходил бесплатно. Параметр больше не
    принимается от клиента вовсе."""
    seen = {}

    async def _capture(alias, messages, allowed_aliases=None, **kwargs):
        seen.update(kwargs)
        return _stream_without_usage()

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _capture)

    _signup(client, "audit_stream@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "привет"}],
            "stream": True,
            "stream_options": {"include_usage": False},
        },
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())

    # json.dumps экранирует не-ASCII (ц...), поэтому текст в потоке
    # латиницей — иначе проверка ищет одно, а в SSE лежит другое.
    assert "answer" in body, "ответ до клиента не дошёл — проверяется не то"
    assert seen.get("stream_options") == {"include_usage": True}, (
        "клиентское значение stream_options долетело до провайдера"
    )


def test_stream_without_usage_is_still_charged(client, monkeypatch):
    """Второй рубеж: даже если поставщик сам не прислал usage, событие нельзя
    закрывать бесплатно — расход у него реальный. Считаем по отданному тексту
    и помечаем оценкой."""

    async def _no_usage(alias, messages, allowed_aliases=None, **kwargs):
        return _stream_without_usage()

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _no_usage)

    _signup(client, "audit_nousage@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    customer = _customer("audit_nousage@test.local")
    before = customer.balance_rub

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "привет"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        "".join(r.iter_text())

    event = _last_event(customer.id)
    assert event is not None
    assert event.status == "success"
    assert event.charged_rub is not None, "вызов закрыт бесплатно"
    assert event.charged_rub > 0
    assert event.billing_estimated is True, "оценка не помечена как оценка"
    assert _customer("audit_nousage@test.local").balance_rub < before


# ---------- 2. n в резерве ----------


def test_n_multiplies_the_reserve():
    """Оплачиваются ВСЕ варианты ответа, а потолок длины режет каждый по
    отдельности: без множителя клиент с n=10 резервирует десятую часть того,
    что спишется."""
    one = pricing.estimate_output_tokens_hint({"max_tokens": 1000})
    ten = pricing.estimate_output_tokens_hint({"max_tokens": 1000, "n": 10})
    assert ten == one * 10

    assert pricing.estimate_output_tokens_hint({"max_tokens": 1000, "n": 1}) == one
    assert pricing.estimate_output_tokens_hint({"max_tokens": 1000, "n": 0}) == one
    assert pricing.estimate_output_tokens_hint({"max_tokens": 1000, "n": "много"}) == one


def test_reserve_grows_with_n(client):
    """Та же проверка на уровне резерва: важно не число токенов само по себе,
    а что зарезервированная сумма растёт."""

    async def _price():
        async with SessionLocal() as session:
            provider, model = llm.resolve_alias("gpt-5-mini")
            cfg = await billing.get_pricing_config(session)
            price = await pricing.find_price(session, provider, model, None, None, utcnow())
            return price, cfg

    price, cfg = asyncio.run(_price())
    messages = [{"role": "user", "content": "привет"}]
    plain = billing.estimate_reserve_rub(price, messages, {"max_tokens": 500}, cfg)
    many = billing.estimate_reserve_rub(price, messages, {"max_tokens": 500, "n": 8}, cfg)
    assert many > plain


# ---------- 3. двойное подтверждение пополнения ----------


def test_topup_is_credited_once_even_on_a_second_confirm(client):
    """Проверка «ещё requested» в маршруте и запись в billing — разные
    моменты; между ними помещается второй такой же запрос."""
    _signup(client, "audit_topup@test.local")
    customer = _customer("audit_topup@test.local")
    admin = _admin_client()
    topup_id = _topup(client, admin, "1000")

    second = admin.post(f"/admin/topups/{topup_id}/confirm")
    assert second.status_code == 409, "повторное подтверждение прошло"

    async def _state():
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(WalletLedger).where(WalletLedger.topup_request_id == topup_id)
                    )
                )
                .scalars()
                .all()
            )
            fresh = await session.get(Customer, customer.id)
            return rows, fresh.balance_rub

    rows, balance = asyncio.run(_state())
    assert len(rows) == 1, "по одной заявке две записи в журнале"
    assert balance == Decimal("1000.0000")


def test_a_confirmed_topup_cannot_be_rejected(client):
    _signup(client, "audit_topup2@test.local")
    admin = _admin_client()
    topup_id = _topup(client, admin, "300")
    assert admin.post(f"/admin/topups/{topup_id}/reject").status_code == 409

    async def _status():
        async with SessionLocal() as session:
            return (await session.get(TopupRequest, topup_id)).status

    assert asyncio.run(_status()) == "confirmed"


# ---------- 4. фолбэк не уходит на модель без цены ----------


def test_fallback_never_goes_to_an_unpriced_model(client, monkeypatch):
    """Цена проверяется для ЗАПРОШЕННОЙ модели, а списание идёт по фактически
    ответившей. Фолбэк на модель без прайса давал бесплатный вызов: cost_usd
    None → charged_rub NULL → ни списания, ни записи в журнал.

    В тестовой базе проценён только gpt-5-mini, gemini-flash (его фолбэк) —
    нет. Значит цепочка должна оборваться, а не выдать бесплатный ответ.
    """
    import litellm

    real = llm.chat_completion
    calls = []

    async def _flaky(alias, messages, **kwargs):
        calls.append(alias)
        if alias == "gpt-5-mini":
            raise litellm.RateLimitError(
                message="simulated", llm_provider="openrouter", model="gpt-5-mini"
            )
        return await real(alias, messages, **kwargs)

    monkeypatch.setattr(llm, "chat_completion", _flaky)

    _signup(client, "audit_fallback@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    customer = _customer("audit_fallback@test.local")
    before = customer.balance_rub

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "бесплатный ответ",
        },
    )
    assert r.status_code >= 500, "непроценённый фолбэк всё-таки ответил"
    assert calls == ["gpt-5-mini"], f"пошли в модель без цены: {calls}"
    assert _customer("audit_fallback@test.local").balance_rub == before

    event = _last_event(customer.id)
    assert event.status == "failed"
    assert event.charged_rub is None


# ---------- 5. mock_response не должен работать в проде ----------


def test_mock_response_is_refused_in_production(client, monkeypatch):
    """Провайдер не вызывается вовсе, ответ выдумывается на месте — а деньги
    списываются настоящие. В тестах параметр нужен, в проде это подарок."""

    assert "mock_response" in _allowed_extra_params()
    monkeypatch.setattr(settings, "environment", "production")
    assert "mock_response" not in _allowed_extra_params()


# ---------- партия 2: защита, которая молча не работала ----------


SECRET = "sk-liveAAAABBBBCCCCDDDD1234567890"


def test_dlp_redacts_secrets_inside_content_parts(client, monkeypatch):
    """Массив частей — стандартный формат OpenAI: его шлют все vision-клиенты
    и собирает сам веб-чат при любой прикреплённой картинке. Пока DLP смотрел
    только на строковый content, защита для этого формата была выключена
    целиком и молча — в истории вызовов стояло «ничего не найдено»."""
    from app.services import dlp

    as_string, found_string = dlp.redact_messages(
        [{"role": "user", "content": f"мой ключ {SECRET}"}]
    )
    as_parts, found_parts = dlp.redact_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"мой ключ {SECRET}"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
                ],
            }
        ]
    )

    assert SECRET not in str(as_string)
    assert SECRET not in str(as_parts), "секрет ушёл провайдеру в открытом виде"
    assert found_parts == found_string, "утечка не отмечена в истории вызовов"
    # картинка проходит нетронутой
    assert as_parts[0]["content"][1]["image_url"] == {"url": "data:image/png;base64,iVBOR"}


def test_message_text_reads_both_shapes():
    from app.services import dlp

    assert dlp.message_text({"content": "просто строка"}) == "просто строка"
    assert (
        dlp.message_text({"content": [{"type": "text", "text": "часть"}, {"type": "image_url"}]})
        == "часть"
    )
    assert dlp.message_text({}) == ""


def test_child_block_list_survives_an_attached_image(client):
    """Блок-лист брал ПОСЛЕДНЕЕ строковое сообщение, а сообщение с картинкой
    уходит массивом частей — ребёнку достаточно было приложить любую
    картинку, чтобы запрет перестал срабатывать, а платил родитель."""
    from app.services.messages import ChildRequestBlocked, prepare_messages

    class _Child:
        is_child = True

    blocked = [{"role": "user", "content": "напиши сочинение про войну и мир"}]
    with pytest.raises(ChildRequestBlocked):
        prepare_messages(_Child(), blocked, None)

    with_image = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "напиши сочинение про войну и мир"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
            ],
        }
    ]
    with pytest.raises(ChildRequestBlocked):
        prepare_messages(_Child(), with_image, None)


def test_healthz_is_degraded_when_no_model_can_be_called(client, monkeypatch):
    """База жива, а вызвать нечего: внешний монитор рапортовал «жив», пока
    сервис не мог обслужить ни одного запроса."""

    async def _nothing(session, now):
        return set()

    monkeypatch.setattr(llm, "priced_aliases", _nothing)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["models_ready"] == 0


def test_empty_model_answer_is_a_failed_call_not_a_paid_one(client, monkeypatch):
    """content: null — штатный отказ модели, choices: [] — сбой поставщика.
    Разбор по индексам ронял обработчик ПОСЛЕ списания: деньги ушли, ответ
    потерян, клиент получил 500."""

    async def _empty(alias, messages, allowed_aliases=None, **kwargs):
        return (
            alias,
            "openrouter",
            "openai/gpt-5-mini",
            {
                "id": "chatcmpl-empty",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        )

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _empty)

    _signup(client, "audit_empty@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    customer = _customer("audit_empty@test.local")
    before = customer.balance_rub

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"})
    assert r.status_code == 502, f"ожидали понятную ошибку, получили {r.status_code}"

    event = _last_event(customer.id)
    assert event.status == "failed", "пустой ответ закрыт как успешный"
    assert event.charged_rub is None, "за пустой ответ списаны деньги"
    assert _customer("audit_empty@test.local").balance_rub == before


def test_no_choices_at_all_does_not_leave_the_reserve_hanging(client, monkeypatch):
    """Падение ДО финализации оставляло событие в pending, и его резерв
    вычитался из доступного баланса до прихода уборщика."""

    async def _no_choices(alias, messages, allowed_aliases=None, **kwargs):
        return alias, "openrouter", "openai/gpt-5-mini", {"id": "x", "choices": []}

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _no_choices)

    _signup(client, "audit_nochoices@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    customer = _customer("audit_nochoices@test.local")

    assert (
        client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"}).status_code
        == 502
    )

    async def _pending():
        async with SessionLocal() as session:
            return (
                (
                    await session.execute(
                        select(UsageEvent).where(
                            UsageEvent.billing_customer_id == customer.id,
                            UsageEvent.status == "pending",
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert asyncio.run(_pending()) == [], "резерв повис в pending"


def test_showroom_links_disappear_with_the_showroom(client, monkeypatch):
    """Моя же вчерашняя регрессия: общая шапка принесла в документацию пять
    ссылок, которые во внутреннем контуре закрыты флагом и отдают 404."""
    monkeypatch.setattr(settings, "enable_public_site", False)
    html = client.get("/docs").text
    for gone in ("/models", "/pricing", "/product/api", "/product/chat", "/solutions/developers"):
        assert f'href="{gone}"' not in html, f"ссылка {gone} ведёт в 404 во внутреннем контуре"
    assert 'href="/docs"' in html, "документация нужна и своим разработчикам"
    assert 'href="/signup"' not in html, "регистрации во внутреннем контуре нет"


# ---------- партия 3: ресурсы и сроки ----------


def test_a_resource_paid_through_today_is_not_expired():
    """«Оплачено до 13.09» значит, что 13 сентября ещё оплачено. Вычитание
    моментов давало ровно суточную ошибку: в 00:01 того же дня ресурс уже
    показывался просроченным — и в кабинете, и в телеграме."""
    from datetime import datetime

    from app.db.models import days_left

    today = datetime(2026, 9, 13, 0, 1, tzinfo=UTC)
    paid_through_today = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    assert days_left(paid_through_today, today) == 0

    tomorrow = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    assert days_left(tomorrow, today) == 1

    yesterday = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
    assert days_left(yesterday, today) == -1

    # и вечером того же дня ответ тот же — момент внутри суток не важен
    late = datetime(2026, 9, 13, 23, 59, tzinfo=UTC)
    assert days_left(paid_through_today, late) == 0
    assert days_left(None, late) is None


def test_resource_state_calls_today_expiring_not_expired(client):
    from datetime import datetime

    from app.db.models import Resource
    from app.services.resources import resource_state

    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    today = Resource(name="x", kind="proxy", expires_at=datetime(2026, 9, 13, tzinfo=UTC))
    assert resource_state(today, now, 7)["code"] == "soon"
    assert resource_state(today, now, 7)["days"] == 0

    gone = Resource(name="x", kind="proxy", expires_at=datetime(2026, 9, 11, tzinfo=UTC))
    assert resource_state(gone, now, 7)["code"] == "expired"
    assert resource_state(gone, now, 7)["days"] == -2, "просрочка завышена на день"


def test_payments_in_other_currencies_are_not_silently_dropped(client):
    """Подписка у зарубежного поставщика — основной сценарий раздела. Итог
    считался только по рублёвым строкам, и оплата картой за $20 показывала
    «0,00 ₽» без единого признака, что часть платежей отброшена."""
    admin = _admin_client()
    _signup(client, "audit_cur@test.local", name="Валютный")
    owner = _customer("audit_cur@test.local")

    admin.post(
        "/admin/resources/new",
        data={
            "kind": "subscription",
            "name": "Подписка за доллары",
            "owner_customer_id": str(owner.id),
            "expires_at": "2027-01-01",
        },
    )

    async def _rid():
        async with SessionLocal() as session:
            from app.db.models import Resource

            return (
                (
                    await session.execute(
                        select(Resource).where(Resource.name == "Подписка за доллары")
                    )
                )
                .scalar_one()
                .id
            )

    rid = asyncio.run(_rid())
    assert admin.post(
        f"/admin/resources/{rid}/pay",
        data={"amount": "20", "currency": "USD", "period_end": "2027-06-01"},
    ).status_code in (200, 303)

    # Режем ровно по строке таблицы, а не по «первым N символам после
    # названия»: разметка строки меняется, окно фиксированной длины от этого
    # ломается, и тест начинает падать на правках вёрстки, а не на дефекте.
    page = admin.get("/admin/resources").text
    # Ищем внутри ТАБЛИЦЫ: название встречается и выше — в сводной строке
    # дашборда «ближайший срок — …», и поиск от начала страницы попадал бы
    # туда, где никаких сумм нет.
    table = page[page.index('class="rtab"') :]
    start = table.rindex("<tr", 0, table.index("Подписка за доллары"))
    row = table[start : table.index("</tr>", start)]
    assert "20,00" in row and "$" in row, "валютный платёж не показан"
    assert "0,00&nbsp;₽" not in row, "вместо платежа показан ноль"


def test_a_typo_in_the_date_can_be_fixed_without_sql(client):
    """Оплата двигает срок только вперёд — это защита от опечатки. Пока
    правки не было вовсе, та же защита делала опечатку неустранимой:
    «оплачено до 2036» навсегда выпадало из предупреждений."""
    admin = _admin_client()
    _signup(client, "audit_typo@test.local", name="Опечаткин")
    owner = _customer("audit_typo@test.local")

    admin.post(
        "/admin/resources/new",
        data={
            "kind": "proxy",
            "name": "Прокси с опечаткой",
            "owner_customer_id": str(owner.id),
            "expires_at": "2036-09-30",
        },
    )

    async def _resource():
        async with SessionLocal() as session:
            from app.db.models import Resource

            return (
                await session.execute(select(Resource).where(Resource.name == "Прокси с опечаткой"))
            ).scalar_one()

    res = asyncio.run(_resource())
    assert res.expires_at.year == 2036

    r = admin.post(
        f"/admin/resources/{res.id}/edit",
        data={
            "name": "Прокси исправленный",
            "owner_customer_id": str(owner.id),
            "account": "login@vpn",
            "url": "",
            "expires_at": "2026-10-01",
            "note": "",
        },
    )
    assert r.status_code in (200, 303)

    fixed = asyncio.run(_resource_by_id(res.id))
    assert fixed.expires_at.year == 2026, "срок не удалось подвинуть назад"
    assert fixed.name == "Прокси исправленный"
    assert fixed.account == "login@vpn"


def test_only_an_admin_can_edit_a_resource(client):
    _signup(client, "audit_noedit@test.local")
    r = client.post(
        "/admin/resources/1/edit",
        data={"name": "чужое", "owner_customer_id": "1"},
    )
    assert r.status_code in (302, 303, 403), f"неадмин правит ресурсы: {r.status_code}"


def _resource_by_id(resource_id):
    async def _get():
        async with SessionLocal() as session:
            from app.db.models import Resource

            return await session.get(Resource, resource_id)

    return _get()


# ---------- партия 4: резерв по цепочке, промпты, телеграм ----------


def test_reserve_covers_the_most_expensive_model_in_the_chain(client, monkeypatch):
    """Резерв брался по цене запрошенной модели, а списывается цена той, что
    фактически ответила. Цепочка по умолчанию ведёт самый дешёвый алиас на
    почти самый дорогой — ×4,5 по выводу, и запаса 1.5 на это не хватает."""
    from datetime import datetime

    from app.db.models import ModelPrice

    async def _add_expensive_fallback():
        async with SessionLocal() as session:
            session.add(
                ModelPrice(
                    provider="openrouter",
                    model="google/gemini-3.5-flash",
                    price_per_1m_input_tokens=Decimal("1.50"),
                    price_per_1m_output_tokens=Decimal("9.00"),
                    valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()

    async def _reserves():
        async with SessionLocal() as session:
            cfg = await billing.get_pricing_config(session)
            messages = [{"role": "user", "content": "привет " * 50}]
            extra = {"max_tokens": 4096}
            chain = await billing.estimate_reserve_for_chain(
                session, "gpt-5-mini", messages, extra, cfg, utcnow()
            )
            provider, model = llm.resolve_alias("gpt-5-mini")
            own_price = await pricing.find_price(session, provider, model, None, None, utcnow())
            own = billing.estimate_reserve_rub(own_price, messages, extra, cfg)
            return chain, own

    asyncio.run(_add_expensive_fallback())
    try:
        chain, own = asyncio.run(_reserves())
        assert chain > own, "резерв не учитывает более дорогую запасную модель"
    finally:
        # Прайс общий на весь прогон: убираем за собой, иначе соседние тесты
        # («claude-sonnet и gemini-flash без прайса») начнут падать.
        async def _cleanup():
            async with SessionLocal() as session:
                row = (
                    (
                        await session.execute(
                            select(ModelPrice).where(ModelPrice.model == "google/gemini-3.5-flash")
                        )
                    )
                    .scalars()
                    .first()
                )
                if row is not None:
                    await session.delete(row)
                    await session.commit()

        asyncio.run(_cleanup())


def test_prompt_fee_counts_towards_the_spend_limit(client):
    """Вся арифметика «сколько потрачено» построена на charged_rub. Пока
    плата за промпт шла мимо, бюджетный потолок её не видел вовсе, а клиент
    в кабинете видел почти нулевой расход при вычерпанном кошельке."""
    from app.db.models import Prompt
    from app.db.models import UsageEvent as UE

    _signup(client, "audit_promptfee@test.local")
    payer = _customer("audit_promptfee@test.local")

    async def _charge():
        async with SessionLocal() as session:
            author = Customer(
                email="audit_promptauthor@test.local", name="Автор", password_hash="x"
            )
            session.add(author)
            await session.flush()
            prompt = Prompt(
                author_customer_id=author.id,
                title="платный",
                system_prompt="будь краток",
                price_rub=Decimal("300.00"),
            )
            session.add(prompt)
            event = UE(
                customer_id=payer.id,
                billing_customer_id=payer.id,
                provider="openrouter",
                model="openai/gpt-5-mini",
                status="success",
                charged_rub=Decimal("0.0100"),
            )
            session.add(event)
            await session.flush()
            person = await session.get(Customer, payer.id)
            person.balance_rub = Decimal("5000.0000")
            await session.commit()
            await billing.charge_prompt_fee(session, payer.id, prompt, event.id)
            refreshed = await session.get(UE, event.id)
            return refreshed.charged_rub

    charged = asyncio.run(_charge())
    assert charged == Decimal("300.0100"), f"плата за промпт не попала в сумму события: {charged}"


def test_telegram_never_writes_the_bot_token_into_logs():
    """httpx кладёт в текст ошибки полный URL, а в URL Telegram токен стоит
    прямо в пути — любой logger.warning(... %r, e) писал боевой токен в лог."""
    from app.core.config import settings as app_settings
    from app.integrations import telegram_bot

    token = app_settings.telegram_bot_token
    assert token, "тестовое окружение должно задавать токен"
    leaked = RuntimeError(f"GET https://api.telegram.org/bot{token}/getUpdates failed")
    assert token not in telegram_bot.safe_error(leaked)
    assert "<ТОКЕН-СКРЫТ>" in telegram_bot.safe_error(leaked)


def test_long_answers_are_sent_in_full_not_truncated(monkeypatch):
    """text[:4000] молча отбрасывал хвост: человек платил за полный ответ,
    получал обрубок на полуслове и не знал, что ответ продолжался."""
    import asyncio as aio

    from app.integrations import telegram_bot

    sent = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.append(json["text"])

    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", lambda **kw: _FakeClient())

    long_answer = "\n".join(f"строка номер {i}" for i in range(1, 900))
    aio.run(telegram_bot.send_message(1, long_answer))

    assert len(sent) > 1, "длинный ответ ушёл одним куском — значит обрезан"
    assert all(len(chunk) <= 4000 for chunk in sent)
    assert "строка номер 899" in "".join(sent), "хвост ответа потерян"


def test_voice_is_not_transcribed_for_someone_who_cannot_pay(client):
    """Распознавание — платный вызов Whisper нашим ключом, и он шёл до
    единственной проверки: до ограничителя частоты, до потолков, до баланса."""
    from app.services import telegram_chat as chatcore

    _signup(client, "audit_voice@test.local")
    person = _customer("audit_voice@test.local")

    async def _check():
        async with SessionLocal() as session:
            fresh = await session.get(Customer, person.id)
            with pytest.raises(billing.InsufficientBalance):
                await chatcore.ensure_can_spend(session, fresh)

    asyncio.run(_check())


# ---------- история вызовов: то, что обещала витрина ----------


def _make_event(customer_id, api_key_id, model, charged, when=None):
    from app.db.models import UsageEvent as UE

    async def _add():
        async with SessionLocal() as session:
            event = UE(
                customer_id=customer_id,
                billing_customer_id=customer_id,
                api_key_id=api_key_id,
                provider="openrouter",
                model=model,
                status="success",
                input_tokens=100,
                output_tokens=50,
                charged_rub=Decimal(charged),
            )
            if when is not None:
                event.created_at = when
            session.add(event)
            await session.commit()
            return event.id

    return asyncio.run(_add())


def _key_ids(email):
    from app.db.models import ApiKey

    async def _get():
        async with SessionLocal() as session:
            person = (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()
            return [
                k.id
                for k in (
                    await session.execute(select(ApiKey).where(ApiKey.customer_id == person.id))
                )
                .scalars()
                .all()
            ]

    return asyncio.run(_get())


def test_history_can_be_filtered_by_key(client):
    """Витрина обещает агентствам отдельный ключ на клиента и историю по
    каждому ключу: без фильтра обещание было пустым."""
    _signup(client, "audit_hist@test.local")
    person = _customer("audit_hist@test.local")
    client.post("/api-key/regenerate", data={"name": "Проект А"})
    client.post("/api-key/regenerate", data={"name": "Проект Б"})
    first, second = _key_ids("audit_hist@test.local")[:2]

    _make_event(person.id, first, "openai/gpt-5-mini", "10.0000")
    _make_event(person.id, second, "openai/gpt-5-mini", "3.0000")
    _make_event(person.id, None, "openai/gpt-5-mini", "1.0000")  # чат/телеграм

    everything = client.get("/usage").text
    assert "14,00" in _plain(everything), "итог по всем вызовам неверен"

    only_first = client.get(f"/usage?key={first}").text
    assert "10,00" in _plain(only_first)
    assert "14,00" not in _plain(only_first), "фильтр по ключу не сработал"

    no_key = client.get("/usage?key=none").text
    assert "1,00" in _plain(no_key), "вызовы из чата и телеграма потерялись"


def test_someone_elses_key_cannot_be_peeked_at(client):
    """Иначе по номеру ключа можно было бы смотреть расход соседа."""
    _signup(client, "audit_hist_a@test.local")
    client.post("/api-key/regenerate", data={"name": "чужой"})
    foreign = _key_ids("audit_hist_a@test.local")[0]

    _signup(client, "audit_hist_b@test.local")
    assert client.get(f"/usage?key={foreign}").status_code == 404


def test_history_export_opens_in_russian_excel(client):
    """BOM и точка с запятой — иначе Excel открывает файл одной колонкой и
    портит кириллицу."""
    _signup(client, "audit_csv@test.local")
    person = _customer("audit_csv@test.local")
    client.post("/api-key/regenerate", data={"name": "Проект А"})
    key_id = _key_ids("audit_csv@test.local")[0]
    _make_event(person.id, key_id, "openai/gpt-5-mini", "12.3456")

    r = client.get("/usage.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]

    body = r.content.decode("utf-8")
    assert body.startswith("\ufeff"), "нет BOM — Excel испортит кириллицу"
    lines = body.lstrip("\ufeff").strip().splitlines()
    assert lines[0].count(";") >= 6, "разделитель не ; — Excel склеит в одну колонку"
    assert "Проект А" in lines[1], "имя ключа не попало в выгрузку"
    assert "12,3456" in lines[1], "сумма не в русском формате"


def test_export_is_not_cut_by_the_page_limit(client):
    """Страница показывает последние N, выгрузка обязана отдать всё за
    период — иначе к акту приложить нечего."""

    _signup(client, "audit_csvall@test.local")
    person = _customer("audit_csvall@test.local")
    limit = USAGE_PAGE_LIMIT
    for _ in range(3):
        _make_event(person.id, None, "openai/gpt-5-mini", "1.0000")

    async def _count_all():
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(UsageEvent).where(UsageEvent.customer_id == person.id)
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

    total = asyncio.run(_count_all())
    body = client.get("/usage.csv").content.decode("utf-8")
    rows = len(body.strip().splitlines()) - 1  # без заголовка
    assert rows == total
    assert limit >= 1


def test_history_needs_a_login(client):
    client.post("/logout")
    assert client.get("/usage", follow_redirects=False).status_code in (302, 303)
    assert client.get("/usage.csv", follow_redirects=False).status_code in (302, 303)


# ---------- партия 6: безопасность и денежные поля ----------


def test_csv_export_does_not_carry_formulas(client):
    """Имя клиент задаёт себе сам при регистрации, а выгрузку открывает
    бухгалтер. Ячейка, начинающаяся с =, исполняется как формула, и один
    клик отправляет чужие email и балансы из соседних строк наружу."""

    assert csv_cell('=HYPERLINK("https://evil.tld")').startswith("'=")
    assert csv_cell("+1").startswith("'+")
    assert csv_cell("-5").startswith("'-")
    assert csv_cell("@x").startswith("'@")
    assert csv_cell("Иванов") == "Иванов"
    assert csv_cell(None) == ""

    _signup(client, "audit_formula@test.local", name='=HYPERLINK("https://evil.tld","отчёт")')
    admin = _admin_client()
    body = admin.get("/admin/customers.csv").content.decode("utf-8")
    assert "=HYPERLINK" in body, "проверяем не то — имени вообще нет в выгрузке"
    for line in body.splitlines()[1:]:
        for cell in line.split(";"):
            assert not cell.startswith("="), f"формула уехала в выгрузку: {cell[:40]}"


def test_money_fields_understand_a_russian_comma(client):

    assert money_field("1,5", "лимит") == Decimal("1.5")
    assert money_field("1 000,25", "лимит") == Decimal("1000.25")
    assert money_field("  ", "лимит") is None
    assert money_field("10", "лимит") == Decimal("10")

    with pytest.raises(HTTPException) as bad:
        money_field("вагон", "лимит")
    assert bad.value.status_code == 400

    with pytest.raises(HTTPException) as negative:
        money_field("-5", "лимит")
    assert negative.value.status_code == 400


def test_a_comma_in_the_limit_form_is_an_error_not_a_500(client):
    """Раньше «1,5» роняло денежную форму в 500, а «-5» принималось молча и
    ключ навсегда отвечал 429: 0 >= -5 истинно всегда."""
    _signup(client, "audit_limitform@test.local")
    client.post("/api-key/regenerate", data={"name": "k"})
    key_id = _key_ids("audit_limitform@test.local")[0]

    assert client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "1,5", "monthly_limit_rub": ""}
    ).status_code in (200, 303), "русская запятая должна приниматься"

    bad = client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "вагон", "monthly_limit_rub": ""}
    )
    assert bad.status_code == 400, f"нечисловое значение дало {bad.status_code}"

    negative = client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "-5", "monthly_limit_rub": ""}
    )
    assert negative.status_code == 400, "отрицательный лимит принят — ключ заблокирован навсегда"


def test_oversized_request_is_refused_before_anything_is_parsed(client):
    """Форма разбирается раньше проверки сессии, поэтому аноним мог заставить
    сервис принять файл любого размера. Потолок стоит до разбора тела."""
    from app.core.config import settings as app_settings

    client.post("/logout")
    big = b"x" * (app_settings.max_request_body_bytes + 1024)
    r = client.post(
        "/chat/send",
        data={"model": "gpt-5-mini", "message": "привет"},
        files={"file": ("big.bin", big, "application/octet-stream")},
    )
    assert r.status_code == 413, f"тело пропущено дальше: {r.status_code}"


def test_password_reset_queue_cannot_be_flooded(client):
    """Единственный инструмент восстановления пароля — очередь у
    администратора. Настоящая заявка тонула бы среди мусора."""
    from app.core import ratelimit

    ratelimit.login_limiter.reset()
    statuses = []
    for i in range(14):
        r = client.post("/forgot-password", data={"email": f"audit_flood{i}@test.local"})
        statuses.append(r.status_code)
    assert 429 in statuses, "очередь заявок наливается без ограничений"
    ratelimit.login_limiter.reset()


# ---------- партия 7: читаемость и пустые состояния ----------


def _oklch_to_srgb(L, C, H):
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    long_component, m, s = l_**3, m_**3, s_**3
    r = +4.0767416621 * long_component - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * long_component + 2.6097574011 * m - 0.3413193965 * s
    bl = -0.0041960863 * long_component - 0.7034186147 * m + 1.7076147010 * s

    def enc(x):
        x = max(0.0, min(1.0, x))
        return 1.055 * (x ** (1 / 2.4)) - 0.055 if x > 0.0031308 else 12.92 * x

    return enc(r), enc(g), enc(bl)


def _luminance(token):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _oklch_to_srgb(*token)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block: str) -> dict:
    """Значения oklch-токенов из одного блока base.html."""
    found = {}
    for name, L, C, H in re.findall(
        r"(--[a-z0-9-]+):\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)", block
    ):
        found[name] = (float(L) / 100, float(C), float(H))
    return found


def _theme_block(css: str, selector: str) -> str:
    start = css.index(selector)
    return css[start : css.index("}", start)]


BASE_CSS = (pathlib.Path("app/templates/base.html")).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "selector,bg_token",
    [
        ("  :root{", "--bg"),
        ('  :root[data-theme="dark"]{', "--bg"),
        ("  .surface-dark{", "--bg"),
        ("  :root{", "--surface"),
        ('  :root[data-theme="dark"]{', "--surface"),
        ("  .surface-dark{", "--surface"),
    ],
)
def test_faint_text_is_readable_in_every_theme(selector, bg_token):
    """--text-faint красит ВСЕ заголовки таблиц, подписи и пояснения в
    продукте. Он был ниже нормы 4.5:1 в обеих темах сразу (3,15–4,14).
    Контраст считается, а не оценивается на глаз — в этом проекте цвет уже
    один раз подбирали глазом и промахнулись мимо логотипа."""
    tokens = _tokens(_theme_block(BASE_CSS, selector))
    ratio = _contrast(tokens["--text-faint"], tokens[bg_token])
    assert ratio >= 4.5, f"{selector} {bg_token}: контраст {ratio:.2f} ниже нормы"


def test_money_colour_is_readable_on_white():
    """--accent-2 красит суммы в админке — на белом он давал 3,04:1."""
    tokens = _tokens(_theme_block(BASE_CSS, "  :root{"))
    ratio = _contrast(tokens["--accent-2"], tokens["--surface"])
    assert ratio >= 4.5, f"суммы на белом: {ratio:.2f}"


def test_colour_scheme_is_declared_for_native_controls():
    """Без color-scheme браузер считает страницу светлой и рисует свои части
    поверх тёмной вёрстки: белая полоса прокрутки и невидимая иконка
    календаря у полей «Оплачено до» в журнале ресурсов."""
    assert "color-scheme:light" in BASE_CSS
    assert BASE_CSS.count("color-scheme:dark") >= 2, "тёмная тема и витрина обе должны объявить"


def test_landing_bubble_does_not_use_hardcoded_white():
    """На витрине --accent светлый, и белый текст на нём давал 2,27:1."""
    landing = pathlib.Path("app/templates/landing.html").read_text(encoding="utf-8")
    bubble = next(line for line in landing.splitlines() if ".msg-user{" in line)
    assert "#fff" not in bubble, "цвет захардкожен вместо токена"
    assert "--btn-on-accent" in bubble


def test_cabinet_does_not_deny_calls_that_happened(client):
    """Итог считается за 14 дней, а таблица вызовов показывает последние 20
    без ограничения по времени: у поработавшего месяц назад первый экран
    утверждал «Вызовов ещё не было» и тут же показывал вызов со списанием."""
    from datetime import timedelta

    _signup(client, "audit_old@test.local")
    person = _customer("audit_old@test.local")
    _make_event(person.id, None, "openai/gpt-5-mini", "7.7777", when=utcnow() - timedelta(days=30))

    html = client.get("/").text
    assert "Вызовов ещё не было" not in html, "страница отрицает вызов, который сама показывает"
    assert "вызовов не было" in html, "нет честной формулировки про период"


def test_empty_chart_state_is_reachable(client):
    """Ветка «пока нечего показывать» была вложена в условие, дублирующее
    её собственное, и не отрисовывалась ни при каком состоянии данных —
    вместо объяснения пользователь видел дыру."""
    from datetime import timedelta

    _signup(client, "audit_chart@test.local")
    person = _customer("audit_chart@test.local")
    _make_event(person.id, None, "openai/gpt-5-mini", "1.0000", when=utcnow() - timedelta(days=40))

    html = client.get("/").text
    body = html[html.index("</style>") :]
    assert "chart-empty" in body, "пустое состояние графика по-прежнему недостижимо"
