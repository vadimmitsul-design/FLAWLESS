"""Автотесты на ежедневную сверку и контроль маржи по моделям (1.5-1.6
доработок). Событие usage_events собирается напрямую (не через billing.*),
чтобы точно контролировать cost_usd/litellm_cost/created_at и тестировать
именно логику отчёта, а не биллинг целиком (он уже покрыт в
test_billing_reserves.py). Хелперы продублированы из других test_*.py
намеренно (см. обоснование в test_features_wave2.py)."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Customer, UsageEvent

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


def _customer_id(email):
    async def _get():
        async with SessionLocal() as session:
            customer = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one()
            return customer.id

    return asyncio.run(_get())


def _make_event(customer_id, model, cost_usd, litellm_cost, charged_rub, created_at, usd_rub_rate="95.0000"):
    async def _create():
        async with SessionLocal() as session:
            event = UsageEvent(
                customer_id=customer_id,
                billing_customer_id=customer_id,
                provider="openai",
                model=model,
                status="success",
                cost_usd=Decimal(str(cost_usd)),
                litellm_cost=Decimal(str(litellm_cost)) if litellm_cost is not None else None,
                charged_rub=Decimal(str(charged_rub)),
                usd_rub_rate=Decimal(usd_rub_rate),
                markup_percent=Decimal("30.00"),
                input_tokens=100,
                output_tokens=50,
            )
            session.add(event)
            await session.flush()
            event.created_at = created_at
            await session.commit()
            return event.id

    return asyncio.run(_create())


def test_non_admin_blocked_from_reconciliation(client):
    _signup(client, "recon1@test.local")
    r = client.get("/admin/reconciliation")
    assert r.status_code == 403


def test_reconciliation_totals_scoped_to_the_day(client):
    _signup(client, "recon2@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon2@test.local")

    target_day = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    other_day = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)

    _make_event(customer_id, "gpt-5-mini", cost_usd="0.05", litellm_cost="0.05", charged_rub="10.0000", created_at=target_day)
    _make_event(customer_id, "gpt-5-mini", cost_usd="0.08", litellm_cost="0.08", charged_rub="20.0000", created_at=target_day)
    _make_event(customer_id, "gpt-5-mini", cost_usd="99", litellm_cost="99", charged_rub="500.0000", created_at=other_day)

    r = admin.get("/admin/reconciliation?d=2026-03-10")
    assert r.status_code == 200
    assert "30.0000 ₽" in r.text  # revenue: 10+20
    assert "500.0000" not in r.text  # другой день не должен попасть


def test_reconciliation_flags_high_cost_discrepancy(client):
    _signup(client, "recon3@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon3@test.local")

    day = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    # наша себестоимость сильно выше контрольного litellm_cost -> расхождение
    _make_event(
        customer_id, "claude-sonnet", cost_usd="1.00", litellm_cost="0.50", charged_rub="150.0000", created_at=day
    )

    r = admin.get("/admin/reconciliation?d=2026-03-11")
    assert r.status_code == 200
    assert "100.0%" in r.text  # (1.00-0.50)/0.50 = 100%
    assert "⚠ сигнал" in r.text


def test_reconciliation_no_discrepancy_signal_when_within_threshold(client):
    _signup(client, "recon4@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon4@test.local")

    day = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    _make_event(
        customer_id, "gemini-flash", cost_usd="1.00", litellm_cost="0.98", charged_rub="150.0000", created_at=day
    )

    r = admin.get("/admin/reconciliation?d=2026-03-12")
    assert r.status_code == 200
    assert "⚠ сигнал" not in r.text


def test_reconciliation_flags_low_margin_model(client):
    _signup(client, "recon5@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon5@test.local")

    day = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
    # выручка почти равна себестоимости -> маржа ~1%, ниже дефолтного порога 15%
    _make_event(
        customer_id, "gpt-5-mini", cost_usd="1.00", litellm_cost="1.00", charged_rub="96.0000",
        created_at=day, usd_rub_rate="95.0000",
    )

    r = admin.get("/admin/reconciliation?d=2026-03-13")
    assert r.status_code == 200
    assert "ниже порога" in r.text


def test_reconciliation_handles_day_with_no_litellm_cost(client):
    """Стриминговые вызовы не пишут litellm_cost — отчёт не должен падать,
    должен честно показать, что сверить нечего."""
    _signup(client, "recon6@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon6@test.local")

    day = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
    _make_event(customer_id, "gpt-5-mini", cost_usd="0.05", litellm_cost=None, charged_rub="10.0000", created_at=day)

    r = admin.get("/admin/reconciliation?d=2026-03-14")
    assert r.status_code == 200
    assert "не дал litellm_cost" in r.text


def test_overview_by_model_shows_cost_and_margin(client):
    _signup(client, "recon7@test.local")
    admin = _admin_client()
    customer_id = _customer_id("recon7@test.local")

    _make_event(
        customer_id, "gpt-5-mini", cost_usd="0.10", litellm_cost="0.10", charged_rub="20.0000",
        created_at=datetime.now(timezone.utc),
    )

    r = admin.get("/admin/overview")
    assert r.status_code == 200
    assert "Маржа по моделям" in r.text
    assert "gpt-5-mini" in r.text
