"""Автотесты на прямые денежные операции админа (пункт 2 плана по итогам
аудита 2026-09-07): начисление бюджета без встречной заявки, корректировка
баланса с указанием автора, редактирование наценки и курса из интерфейса.
Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app import billing
from app.db import SessionLocal
from app.models import Customer, PricingConfig, WalletLedger

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


def _ledger(customer_id):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(WalletLedger)
                    .where(WalletLedger.customer_id == customer_id)
                    .order_by(WalletLedger.created_at.desc())
                )
            ).scalars().all()

    return asyncio.run(_get())


def _pricing():
    async def _get():
        async with SessionLocal() as session:
            return (await session.execute(select(PricingConfig))).scalar_one()

    return asyncio.run(_get())


# ---------- начисление бюджета ----------


def test_admin_credits_budget_without_any_request_from_the_customer(client):
    _signup(client, "money1@test.local")
    target = _customer("money1@test.local")
    assert target.balance_rub == 0

    admin = _admin_client()
    r = admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "5000.00", "entry_type": "adjustment", "note": "бюджет на сентябрь"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert _customer("money1@test.local").balance_rub == Decimal("5000.0000")


def test_manual_credit_is_recorded_in_the_ledger_with_its_author(client):
    _signup(client, "money2@test.local")
    target = _customer("money2@test.local")
    admin_row = _customer(ADMIN_EMAIL)

    admin = _admin_client()
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "1200.50", "entry_type": "adjustment", "note": "бюджет Q3"},
    )

    entries = _ledger(target.id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.delta_rub == Decimal("1200.5000")
    assert entry.entry_type == "adjustment"
    assert entry.note == "бюджет Q3"
    assert entry.created_by_admin_id == admin_row.id


def test_negative_adjustment_takes_money_back(client):
    _signup(client, "money3@test.local")
    target = _customer("money3@test.local")
    admin = _admin_client()

    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "300", "entry_type": "adjustment", "note": "выдали"},
    )
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "-100", "entry_type": "adjustment", "note": "ошиблись, забираем"},
    )
    assert _customer("money3@test.local").balance_rub == Decimal("200.0000")
    assert len(_ledger(target.id)) == 2


def _cash_total():
    """Ровно то, что /admin/overview показывает как «Касса»: сумма записей
    типа topup. Проверяем величину, а не текст страницы — начисленный бюджет
    законно появляется в плитке «Обязательство», и поиск числа по HTML
    ловил бы именно её."""

    async def _get():
        async with SessionLocal() as session:
            from sqlalchemy import func

            return (
                await session.execute(
                    select(func.coalesce(func.sum(WalletLedger.delta_rub), 0)).where(
                        WalletLedger.entry_type == "topup"
                    )
                )
            ).scalar_one()

    return asyncio.run(_get())


def test_budget_grant_is_not_counted_as_cash_but_topup_is(client):
    """entry_type различает смысл: «Касса» суммирует только topup, поэтому
    внутренний бюджет не должен её раздувать, а реальная оплата — должна."""
    _signup(client, "money4@test.local")
    target = _customer("money4@test.local")
    admin = _admin_client()

    cash_before = _cash_total()

    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "777", "entry_type": "adjustment", "note": "внутренний бюджет"},
    )
    assert _cash_total() == cash_before
    assert _customer("money4@test.local").balance_rub == Decimal("777.0000")

    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "777", "entry_type": "topup", "note": "оплата по счёту"},
    )
    assert _cash_total() == cash_before + Decimal("777.0000")

    assert admin.get("/admin/overview").status_code == 200


def test_credited_budget_actually_lets_the_developer_call(client):
    _signup(client, "money5@test.local")
    target = _customer("money5@test.local")

    r = client.post("/api-key/regenerate", data={"name": "dev"})
    import re

    api_key = re.search(r"nh_[A-Za-z0-9_-]+", r.text).group(0)

    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "mock_response": "ok",
    }
    blocked = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload
    )
    assert blocked.status_code == 402  # нулевой баланс

    admin = _admin_client()
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "500", "entry_type": "adjustment", "note": "бюджет"},
    )

    allowed = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload
    )
    assert allowed.status_code == 200


def test_balance_change_requires_a_reason(client):
    _signup(client, "money6@test.local")
    target = _customer("money6@test.local")
    admin = _admin_client()
    r = admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "100", "entry_type": "adjustment", "note": "   "},
    )
    assert r.status_code == 400
    assert _customer("money6@test.local").balance_rub == 0


def test_zero_and_bogus_entry_type_are_rejected(client):
    _signup(client, "money7@test.local")
    target = _customer("money7@test.local")
    admin = _admin_client()

    assert (
        admin.post(
            f"/admin/customers/{target.id}/balance",
            data={"amount_rub": "0", "entry_type": "adjustment", "note": "ничего"},
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/customers/{target.id}/balance",
            data={"amount_rub": "50", "entry_type": "usage", "note": "подделка"},
        ).status_code
        == 400
    )
    assert _customer("money7@test.local").balance_rub == 0


def test_non_admin_cannot_change_anyone_balance(client):
    _signup(client, "money8@test.local")
    victim = _customer("money8@test.local")

    attacker = _new_client()
    _signup(attacker, "money9@test.local")
    r = attacker.post(
        f"/admin/customers/{victim.id}/balance",
        data={"amount_rub": "100000", "entry_type": "topup", "note": "себе"},
    )
    assert r.status_code == 403
    assert _customer("money8@test.local").balance_rub == 0


def test_customer_detail_page_shows_balance_and_ledger(client):
    _signup(client, "money10@test.local")
    target = _customer("money10@test.local")
    admin = _admin_client()
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "42", "entry_type": "adjustment", "note": "тестовое начисление"},
    )
    page = admin.get(f"/admin/customers/{target.id}")
    assert page.status_code == 200
    assert "тестовое начисление" in page.text
    assert "42.0000" in page.text


# ---------- наценка и курс ----------


def test_admin_can_change_markup_and_rate(client):
    admin = _admin_client()
    admin_row = _customer(ADMIN_EMAIL)
    original = _pricing()

    r = admin.post(
        "/admin/pricing",
        data={"markup_percent": "42.00", "usd_rub_rate": "101.5000"},
        follow_redirects=True,
    )
    assert r.status_code == 200

    updated = _pricing()
    assert updated.markup_percent == Decimal("42.00")
    assert updated.usd_rub_rate == Decimal("101.5000")
    assert updated.updated_by_admin_id == admin_row.id

    # вернуть как было, чтобы не влиять на другие тесты в общей БД
    admin.post(
        "/admin/pricing",
        data={
            "markup_percent": f"{original.markup_percent}",
            "usd_rub_rate": f"{original.usd_rub_rate}",
        },
    )


def test_negative_markup_and_zero_rate_are_rejected(client):
    admin = _admin_client()
    before = _pricing()

    assert (
        admin.post(
            "/admin/pricing", data={"markup_percent": "-10", "usd_rub_rate": "95"}
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/admin/pricing", data={"markup_percent": "30", "usd_rub_rate": "0"}
        ).status_code
        == 400
    )

    after = _pricing()
    assert after.markup_percent == before.markup_percent
    assert after.usd_rub_rate == before.usd_rub_rate


def test_pricing_change_does_not_touch_past_charges(client):
    """Наценка и курс копируются в UsageEvent в момент вызова — уже сделанные
    списания не должны пересчитываться при смене настроек."""
    import re

    _signup(client, "money11@test.local")
    target = _customer("money11@test.local")
    admin = _admin_client()
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "500", "entry_type": "adjustment", "note": "бюджет"},
    )
    api_key = re.search(
        r"nh_[A-Za-z0-9_-]+", client.post("/api-key/regenerate", data={"name": "k"}).text
    ).group(0)
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "ok",
        },
    )
    charged_before = _customer("money11@test.local").balance_rub
    original = _pricing()

    admin.post("/admin/pricing", data={"markup_percent": "500", "usd_rub_rate": "1000"})
    assert _customer("money11@test.local").balance_rub == charged_before

    admin.post(
        "/admin/pricing",
        data={
            "markup_percent": f"{original.markup_percent}",
            "usd_rub_rate": f"{original.usd_rub_rate}",
        },
    )


def test_non_admin_cannot_reach_pricing(client):
    _signup(client, "money12@test.local")
    assert client.get("/admin/pricing").status_code == 403
    assert (
        client.post(
            "/admin/pricing", data={"markup_percent": "0", "usd_rub_rate": "1"}
        ).status_code
        == 403
    )


def test_billing_helper_rejects_unknown_customer():
    async def _run():
        async with SessionLocal() as session:
            try:
                await billing.admin_adjust_balance(
                    session,
                    customer_id=999999,
                    delta_rub=Decimal("10"),
                    entry_type="adjustment",
                    note="нет такого",
                    admin_id=1,
                )
            except ValueError:
                return True
            return False

    assert asyncio.run(_run()) is True
