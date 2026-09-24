"""Автотесты на пункты 5-6 плана по итогам аудита 2026-09-07:
защита от бесплатного расхода (модель без цены, безлимитная длина ответа) и
эксплуатационные вещи (живая проверка здоровья, оповещения о сбоях).
Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.db.models import Customer, ModelPrice, UsageEvent
from app.integrations import llm
from app.services import billing, pricing
from app.workers import alerts

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _admin_client():
    from fastapi.testclient import TestClient

    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _fund(client, admin, email, amount="10000"):
    target = _customer(email)
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": amount, "entry_type": "adjustment", "note": "бюджет для теста"},
    )
    return target


def _issue_key(client):
    r = client.post("/api-key/regenerate", data={"name": "k"})
    return re.search(r"nh_[A-Za-z0-9_-]+", r.text).group(0)


CALL = {
    "model": "gpt-5-mini",
    "messages": [{"role": "user", "content": "hi"}],
    "mock_response": "ok",
}


# ---------- 5a: модель без действующей цены ----------


def test_call_to_unpriced_model_is_refused_not_served_for_free(client):
    """claude-sonnet есть в реестре моделей, но не засеян в тестовом прайсе.
    Раньше такой вызов проходил и был бесплатным: себестоимость None ->
    списания нет -> баланс не падает -> повторяй сколько угодно."""
    _signup(client, "free1@test.local")
    admin = _admin_client()
    _fund(client, admin, "free1@test.local")
    api_key = _issue_key(client)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={**CALL, "model": "claude-sonnet"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["type"] == "model_not_priced"


def test_unpriced_model_creates_no_usage_event_at_all(client):
    _signup(client, "free2@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "free2@test.local")
    api_key = _issue_key(client)

    async def _count():
        async with SessionLocal() as session:
            from sqlalchemy import func

            return (
                await session.execute(
                    select(func.count(UsageEvent.id)).where(
                        UsageEvent.billing_customer_id == target.id
                    )
                )
            ).scalar_one()

    before = asyncio.run(_count())
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={**CALL, "model": "claude-sonnet"},
    )
    assert asyncio.run(_count()) == before  # отказ ДО резерва и вызова провайдера


def test_priced_model_still_works(client):
    _signup(client, "free3@test.local")
    admin = _admin_client()
    _fund(client, admin, "free3@test.local")
    api_key = _issue_key(client)

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 200


def test_web_chat_refuses_unpriced_model(client, monkeypatch):
    async def _fake(alias, messages, **kwargs):
        raise AssertionError("до провайдера доходить не должно")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake)

    _signup(client, "free4@test.local")
    admin = _admin_client()
    _fund(client, admin, "free4@test.local")

    r = client.post("/chat/send", data={"model": "claude-sonnet", "message": "привет"})
    assert r.status_code == 503
    assert "цена" in r.json()["detail"].lower()


def test_expired_price_row_counts_as_unpriced(client):
    """Строка цены с истёкшим valid_until — та же дыра, что и её отсутствие."""

    async def _add_expired():
        async with SessionLocal() as session:
            session.add(
                ModelPrice(
                    provider="openai",
                    model="expired-model",
                    price_per_1m_input_tokens=Decimal("1"),
                    price_per_1m_output_tokens=Decimal("2"),
                    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                    valid_until=datetime(2021, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()

    asyncio.run(_add_expired())

    async def _check():
        async with SessionLocal() as session:
            with pytest.raises(billing.ModelNotPriced):
                await billing.price_for_call(session, "openai", "expired-model", datetime.now(UTC))

    asyncio.run(_check())


# ---------- 5b: потолок длины ответа ----------


def test_output_cap_is_always_sent_to_the_provider(client, monkeypatch):
    captured = {}

    async def _capture(alias, messages, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _capture)

    _signup(client, "cap1@test.local")
    admin = _admin_client()
    _fund(client, admin, "cap1@test.local")
    api_key = _issue_key(client)

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert captured.get("max_tokens") == settings.max_output_tokens_cap


def test_client_request_above_the_cap_is_clamped_down(client, monkeypatch):
    captured = {}

    async def _capture(alias, messages, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _capture)

    _signup(client, "cap2@test.local")
    admin = _admin_client()
    _fund(client, admin, "cap2@test.local")
    api_key = _issue_key(client)

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 999999,
        },
    )
    assert captured.get("max_tokens") == settings.max_output_tokens_cap


def test_client_request_below_the_cap_is_respected(client, monkeypatch):
    captured = {}

    async def _capture(alias, messages, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _capture)

    _signup(client, "cap3@test.local")
    admin = _admin_client()
    _fund(client, admin, "cap3@test.local")
    api_key = _issue_key(client)

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100,
        },
    )
    assert captured.get("max_tokens") == 100


def test_clamp_handles_the_alternate_parameter_name():
    clamped = pricing.clamp_output_tokens({"max_completion_tokens": 999999})
    assert clamped["max_completion_tokens"] == settings.max_output_tokens_cap
    assert "max_tokens" not in clamped


def test_reserve_uses_the_same_capped_number_as_the_request():
    """Оценка и факт должны сходиться по построению: обе читают один extra."""
    extra = pricing.clamp_output_tokens({"max_tokens": 999999})
    assert pricing.estimate_output_tokens_hint(extra) == settings.max_output_tokens_cap


# ---------- 6: эксплуатация ----------


def test_healthz_actually_touches_the_database(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # Готовность моделей теперь тоже часть ответа: живая база при нулевом
    # каталоге означает сервис, который не может обслужить ни одного вызова.
    assert body["models_ready"] >= 1


def test_alerts_fire_on_calls_without_cost(client):
    """Вызовы без себестоимости = расход у провайдера без списания."""
    _signup(client, "alert1@test.local")
    target = _customer("alert1@test.local")

    async def _make_uncosted():
        async with SessionLocal() as session:
            session.add(
                UsageEvent(
                    customer_id=target.id,
                    billing_customer_id=target.id,
                    provider="openai",
                    model="gpt-5-mini",
                    status="success",
                    cost_usd=None,
                    charged_rub=None,
                )
            )
            await session.commit()

    asyncio.run(_make_uncosted())

    async def _collect():
        async with SessionLocal() as session:
            return await alerts.collect_problems(session)

    problems = asyncio.run(_collect())
    assert any(key == "uncosted" for key, _ in problems)


def test_alerts_stay_quiet_when_nothing_is_wrong(monkeypatch):
    """Раньше здесь не было ни одного assert — тест вызывал функцию и не
    смотрел результат.

    И посылка «старые события других тестов в часовое окно не попадают» была
    ЛОЖНОЙ: база одна на весь прогон, соседние тесты успевают наделать и
    вызовов без себестоимости, и ошибок — в окно они попадают все. Поэтому
    окно уводится в заведомо пустое время: смотрим на час, в котором ничего
    не происходило, и требуем полной тишины."""
    from datetime import datetime

    quiet_hour = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(alerts, "utcnow", lambda: quiet_hour)
    # Раздел ресурсов выключаем отдельно: у него окно смотрит НАЗАД без
    # нижней границы (expires_at <= now+7дн), поэтому в будущем часе в него
    # попадают все ресурсы, заведённые другими тестами. Тишину по ресурсам
    # проверяет tests/test_resources.py на своих данных.
    monkeypatch.setattr(alerts.settings, "enable_resources", False)

    async def _consistent_wallets(session):
        return []

    # This test isolates time-window alerts. Reconciliation covers all history
    # and has separate tests with internally consistent, rollback-only fixtures.
    monkeypatch.setattr(alerts, "find_wallet_mismatches", _consistent_wallets)

    async def _collect():
        async with SessionLocal() as session:
            return await alerts.collect_problems(session)

    problems = asyncio.run(_collect())
    assert problems == [], f"тревога на час, в котором ничего не было: {problems}"


def test_alert_cooldown_prevents_repeat_spam():
    alerts.default_cooldown.reset()
    assert alerts.default_cooldown.allow("проба") is True
    assert alerts.default_cooldown.allow("проба") is False  # повтор подавлен
    assert alerts.default_cooldown.allow("другая") is True  # другой ключ независим
    alerts.default_cooldown.reset()
