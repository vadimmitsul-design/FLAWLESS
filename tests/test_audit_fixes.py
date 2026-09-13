# -*- coding: utf-8 -*-
"""Находки аудита 2026-09-13, партия 1: деньги.

Общее у всех четырёх — вызов проходит, поставщику платим мы, а с клиента не
списывается ничего. Отдаётся при этом честный 200, поэтому ни один тест на
коды ответа их не видел.

Префикс адресов — `audit_`: тестовая база одна на весь прогон, и совпадение
email с другим файлом роняет регистрацию 409-м (в проекте так уже обжигались).
"""

import asyncio
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import billing, llm, pricing
from app.config import settings
from app.db import SessionLocal
from app.models import Customer, TopupRequest, UsageEvent, WalletLedger, utcnow

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
                await session.execute(
                    select(UsageEvent)
                    .where(UsageEvent.billing_customer_id == customer_id)
                    .order_by(UsageEvent.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

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
                await session.execute(
                    select(WalletLedger).where(WalletLedger.topup_request_id == topup_id)
                )
            ).scalars().all()
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
    from app import main

    assert "mock_response" in main._allowed_extra_params()
    monkeypatch.setattr(settings, "environment", "production")
    assert "mock_response" not in main._allowed_extra_params()
