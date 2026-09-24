"""Автотесты на потолок расхода НА ЧЕЛОВЕКА (пункт 4 плана по итогам аудита
2026-09-07). Главное, что здесь проверяется: лимит нельзя обойти ни выпуском
второго ключа, ни переходом в веб-чат или Telegram — раньше проверка жила
только в /v1/chat/completions и обходилась в один клик.
Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import ratelimit
from app.db import SessionLocal
from app.db.models import Customer, UsageEvent
from app.integrations import llm
from app.services import billing
from app.services import telegram_chat as chatcore

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


def _new_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _issue_key(client, name="k"):
    r = client.post("/api-key/regenerate", data={"name": name})
    assert r.status_code == 200
    return re.search(r"nh_[A-Za-z0-9_-]+", r.text).group(0)


def _fund(client, admin, email, amount="10000"):
    target = _customer(email)
    r = admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": amount, "entry_type": "adjustment", "note": "бюджет для теста"},
    )
    assert r.status_code in (200, 303)
    return target


def _set_customer_limits(admin, customer_id, daily="", monthly=""):
    r = admin.post(
        f"/admin/customers/{customer_id}/limits",
        data={"daily_limit_rub": daily, "monthly_limit_rub": monthly},
    )
    assert r.status_code in (200, 303)


def _spend(payer_id, amount, when=None, api_key_id=None):
    """Проставляем уже состоявшийся расход, чтобы не гонять реальные вызовы:
    лимит считается по charged_rub."""

    async def _create():
        async with SessionLocal() as session:
            event = UsageEvent(
                customer_id=payer_id,
                billing_customer_id=payer_id,
                api_key_id=api_key_id,
                provider="openai",
                model="gpt-5-mini",
                status="success",
                cost_usd=Decimal("0.01"),
                charged_rub=Decimal(amount),
                usd_rub_rate=Decimal("95.0000"),
                markup_percent=Decimal("30.00"),
            )
            session.add(event)
            await session.flush()
            if when is not None:
                event.created_at = when
            await session.commit()

    asyncio.run(_create())


CALL = {
    "model": "gpt-5-mini",
    "messages": [{"role": "user", "content": "hi"}],
    "mock_response": "ok",
}


# ---------- потолок на человека закрывает все три двери ----------


def test_customer_monthly_limit_blocks_the_api(client):
    _signup(client, "lim1@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim1@test.local")
    api_key = _issue_key(client)
    _set_customer_limits(admin, target.id, monthly="100")
    _spend(target.id, "150")  # уже потрачено больше потолка

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 429
    body = r.json()["detail"]["error"]
    assert body["type"] == "spend_limit_exceeded"
    assert body["scope"] == "customer"


def test_customer_limit_is_not_reset_by_issuing_another_key(client):
    """Раньше лимит жил только на ключе — исчерпал, выпустил второй и работай."""
    _signup(client, "lim2@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim2@test.local")
    first_key = _issue_key(client, "первый")
    _set_customer_limits(admin, target.id, monthly="100")
    _spend(target.id, "150", api_key_id=None)

    assert (
        client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {first_key}"}, json=CALL
        ).status_code
        == 429
    )

    second_key = _issue_key(client, "второй")
    assert second_key != first_key
    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {second_key}"}, json=CALL
    )
    assert r.status_code == 429  # новый ключ не даёт чистого лимита


def test_customer_limit_blocks_the_web_chat(client, monkeypatch):
    """Веб-чат не проверял вообще ничего — лимит обходился переходом сюда."""

    async def _fake_call(alias, messages, **kwargs):
        raise AssertionError("до провайдера доходить не должно — лимит исчерпан")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "lim3@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim3@test.local")
    _set_customer_limits(admin, target.id, monthly="100")
    _spend(target.id, "150")

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"})
    assert r.status_code == 429
    assert "лимит" in r.json()["detail"].lower()


def test_customer_limit_blocks_telegram(client):
    _signup(client, "lim4@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim4@test.local")
    _set_customer_limits(admin, target.id, monthly="100")
    _spend(target.id, "150")

    async def _run():
        async with SessionLocal() as session:
            await chatcore.run_chat_turn(
                session, target.id, "gpt-5-mini", [{"role": "user", "content": "hi"}]
            )

    with pytest.raises(billing.SpendLimitExceeded):
        asyncio.run(_run())


def test_spend_from_the_web_chat_counts_against_the_same_limit(client):
    """Потолок общий: расход, сделанный через чат, закрывает и API тоже."""
    _signup(client, "lim5@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim5@test.local")
    api_key = _issue_key(client)
    _set_customer_limits(admin, target.id, monthly="100")
    _spend(target.id, "150", api_key_id=None)  # события чата пишутся без ключа

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 429


# ---------- границы периодов и взаимодействие с лимитом ключа ----------


def test_daily_limit_ignores_spend_from_previous_days(client):
    _signup(client, "lim6@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim6@test.local")
    api_key = _issue_key(client)
    _set_customer_limits(admin, target.id, daily="100")
    _spend(target.id, "500", when=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))  # давно

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 200


def test_key_limit_still_applies_inside_the_customer_limit(client):
    _signup(client, "lim7@test.local")
    admin = _admin_client()
    target = _fund(client, admin, "lim7@test.local")
    api_key = _issue_key(client)
    key_id = int(re.search(r"/api-keys/(\d+)/limits", client.get("/").text).group(1))
    _set_customer_limits(admin, target.id, monthly="100000")  # общий потолок высокий
    client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "10", "monthly_limit_rub": ""}
    )
    _spend(target.id, "50", api_key_id=key_id)

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 429
    assert r.json()["detail"]["error"]["scope"] == "key"


def test_no_limits_configured_means_no_blocking(client):
    _signup(client, "lim8@test.local")
    admin = _admin_client()
    _fund(client, admin, "lim8@test.local")
    api_key = _issue_key(client)

    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=CALL
    )
    assert r.status_code == 200


def test_child_spend_counts_against_the_parent_limit(client):
    _signup(client, "limparent@test.local")
    admin = _admin_client()
    parent = _fund(client, admin, "limparent@test.local")
    client.post(
        "/children/new",
        data={"email": "limkid@test.local", "name": "Ребёнок", "password": "KidPass123"},
        follow_redirects=True,
    )
    _set_customer_limits(admin, parent.id, monthly="100")
    _spend(parent.id, "150")

    kid = _new_client()
    kid.post("/login", data={"email": "limkid@test.local", "password": "KidPass123"})
    kid_key = _issue_key(kid, "детский")
    r = kid.post("/v1/chat/completions", headers={"Authorization": f"Bearer {kid_key}"}, json=CALL)
    assert r.status_code == 429
    assert r.json()["detail"]["error"]["scope"] == "customer"


# ---------- управление лимитом ----------


def test_admin_sets_and_clears_the_limit(client):
    _signup(client, "lim9@test.local")
    admin = _admin_client()
    target = _customer("lim9@test.local")

    _set_customer_limits(admin, target.id, daily="50", monthly="900")
    updated = _customer("lim9@test.local")
    assert updated.daily_limit_rub == Decimal("50.0000")
    assert updated.monthly_limit_rub == Decimal("900.0000")

    _set_customer_limits(admin, target.id, daily="", monthly="")
    cleared = _customer("lim9@test.local")
    assert cleared.daily_limit_rub is None
    assert cleared.monthly_limit_rub is None


def test_negative_limit_is_rejected(client):
    _signup(client, "lim10@test.local")
    admin = _admin_client()
    target = _customer("lim10@test.local")
    r = admin.post(
        f"/admin/customers/{target.id}/limits",
        data={"daily_limit_rub": "-5", "monthly_limit_rub": ""},
    )
    assert r.status_code == 400
    assert _customer("lim10@test.local").daily_limit_rub is None


def test_customer_cannot_set_their_own_ceiling(client):
    """Потолок на человека — бюджетный контроль компании, не самоограничение."""
    _signup(client, "lim11@test.local")
    target = _customer("lim11@test.local")
    r = client.post(
        f"/admin/customers/{target.id}/limits",
        data={"daily_limit_rub": "999999", "monthly_limit_rub": "999999"},
    )
    assert r.status_code == 403
    assert _customer("lim11@test.local").monthly_limit_rub is None


# ---------- ограничитель частоты ----------


def test_rate_limit_buckets_do_not_collide_between_keys_and_people():
    """Ключ №N и человек №N — разные сущности; на голых целых id они делили
    бы одно ведро счётчика."""
    from app.core.config import settings

    ratelimit.default_limiter.reset()
    for _ in range(settings.rate_limit_per_window):
        assert ratelimit.check(ratelimit.api_key_bucket(777)) is True
    assert ratelimit.check(ratelimit.api_key_bucket(777)) is False
    assert ratelimit.check(ratelimit.customer_bucket(777)) is True


def test_web_chat_is_rate_limited(client, monkeypatch):
    from app.core.config import settings

    async def _fake_call(alias, messages, **kwargs):
        raise AssertionError("не должны дойти до провайдера в этом тесте")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "lim12@test.local")
    target = _customer("lim12@test.local")
    ratelimit.default_limiter.reset()
    for _ in range(settings.rate_limit_per_window):
        ratelimit.check(ratelimit.customer_bucket(target.id))

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"})
    assert r.status_code == 429
