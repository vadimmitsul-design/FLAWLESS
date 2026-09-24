"""Автотесты на отчёт по людям (пункт 3 плана по итогам аудита 2026-09-07):
расход за период по каждому человеку, разбивка по моделям и дням, помесячный
итог, выгрузка в CSV. UsageEvent собирается напрямую, чтобы точно управлять
created_at и суммой — биллинг сам по себе покрыт в test_billing_reserves.py.
Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.db.models import Customer, UsageEvent

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


def _make_event(payer_id, model, charged_rub, created_at, actor_id=None, cost_usd="0.01"):
    async def _create():
        async with SessionLocal() as session:
            event = UsageEvent(
                customer_id=actor_id or payer_id,
                billing_customer_id=payer_id,
                provider="openai",
                model=model,
                status="success",
                cost_usd=Decimal(cost_usd),
                charged_rub=Decimal(charged_rub),
                usd_rub_rate=Decimal("95.0000"),
                markup_percent=Decimal("30.00"),
                input_tokens=100,
                output_tokens=50,
            )
            session.add(event)
            await session.flush()
            event.created_at = created_at
            await session.commit()

    asyncio.run(_create())


MARCH = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
MARCH_OTHER_DAY = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
APRIL = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)


def _csv_row(admin, month, email):
    """Числа сверяем по CSV, а не поиском по HTML: тестовая БД общая на весь
    прогон, и то же самое число легко встречается в чужой строке (например,
    чьим-то балансом) — отрицательные проверки по разметке ложно падают."""
    import csv as _csv

    body = admin.get(f"/admin/customers.csv?month={month}").content.decode("utf-8").lstrip("﻿")
    for row in _csv.DictReader(body.splitlines(), delimiter=";"):
        if row["Email"] == email:
            return row
    raise AssertionError(f"{email} не найден в выгрузке за {month}")


# ---------- список людей ----------


def test_customer_list_shows_spend_for_the_selected_month(client):
    _signup(client, "rep1@test.local", name="Разработчик Один")
    target = _customer("rep1@test.local")
    _make_event(target.id, "gpt-5-mini", "12.5000", MARCH)
    _make_event(target.id, "gpt-5-mini", "7.5000", MARCH_OTHER_DAY)
    _make_event(target.id, "gpt-5-mini", "999.0000", APRIL)  # другой месяц

    admin = _admin_client()
    assert admin.get("/admin/customers?month=2026-03").status_code == 200

    march = _csv_row(admin, "2026-03", "rep1@test.local")
    assert march["Потрачено, ₽"] == "20,0000"  # 12.5 + 7.5, апрельский вызов не влез
    assert march["Вызовов"] == "2"


def test_month_navigation_switches_the_period(client):
    _signup(client, "rep2@test.local")
    target = _customer("rep2@test.local")
    _make_event(target.id, "gpt-5-mini", "42.0000", APRIL)

    admin = _admin_client()
    assert _csv_row(admin, "2026-04", "rep2@test.local")["Потрачено, ₽"] == "42,0000"
    assert _csv_row(admin, "2026-03", "rep2@test.local")["Потрачено, ₽"] == "0,0000"


def test_monthly_total_across_everyone(client):
    _signup(client, "rep3a@test.local")
    second = _new_client()
    _signup(second, "rep3b@test.local")
    a = _customer("rep3a@test.local")
    b = _customer("rep3b@test.local")
    _make_event(a.id, "gpt-5-mini", "100.0000", datetime(2026, 5, 4, 10, 0, tzinfo=UTC))
    _make_event(b.id, "claude-sonnet", "250.0000", datetime(2026, 5, 6, 10, 0, tzinfo=UTC))

    admin = _admin_client()
    page = admin.get("/admin/customers?month=2026-05")
    assert "350.0000" in page.text  # итог за месяц по всем


def test_child_spend_lands_on_the_parent_wallet(client):
    """Расход считается по кошельку: за детский аккаунт платит родитель."""
    _signup(client, "repparent@test.local")
    parent = _customer("repparent@test.local")
    client.post(
        "/children/new",
        data={"email": "repkid@test.local", "name": "Ребёнок", "password": "KidPass123"},
        follow_redirects=True,
    )
    kid = _customer("repkid@test.local")
    _make_event(
        parent.id, "gpt-5-mini", "33.0000", datetime(2026, 6, 3, 10, 0, tzinfo=UTC), actor_id=kid.id
    )

    admin = _admin_client()
    page = admin.get(f"/admin/customers/{parent.id}?month=2026-06")
    assert "33.0000" in page.text


def test_invalid_month_is_rejected(client):
    admin = _admin_client()
    assert admin.get("/admin/customers?month=nonsense").status_code == 400
    assert admin.get("/admin/customers?month=2026-13").status_code == 400


# ---------- карточка человека ----------


def test_detail_breaks_spend_down_by_model(client):
    _signup(client, "rep4@test.local")
    target = _customer("rep4@test.local")
    _make_event(target.id, "gpt-5-mini", "10.0000", datetime(2026, 7, 2, 10, 0, tzinfo=UTC))
    _make_event(target.id, "claude-sonnet", "30.0000", datetime(2026, 7, 3, 10, 0, tzinfo=UTC))

    admin = _admin_client()
    page = admin.get(f"/admin/customers/{target.id}?month=2026-07")
    assert page.status_code == 200
    assert "gpt-5-mini" in page.text
    assert "claude-sonnet" in page.text
    assert "10.0000" in page.text
    assert "30.0000" in page.text
    assert "40.0000" in page.text  # итог за период


def test_detail_breaks_spend_down_by_day(client):
    _signup(client, "rep5@test.local")
    target = _customer("rep5@test.local")
    _make_event(target.id, "gpt-5-mini", "5.0000", datetime(2026, 8, 4, 8, 0, tzinfo=UTC))
    _make_event(target.id, "gpt-5-mini", "6.0000", datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    _make_event(target.id, "gpt-5-mini", "7.0000", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))

    admin = _admin_client()
    page = admin.get(f"/admin/customers/{target.id}?month=2026-08")
    assert "04.08" in page.text
    assert "09.08" in page.text
    assert "11.0000" in page.text  # два вызова 4 августа сложились


def test_detail_shows_lifetime_total_alongside_the_period(client):
    _signup(client, "rep6@test.local")
    target = _customer("rep6@test.local")
    _make_event(target.id, "gpt-5-mini", "15.0000", datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    _make_event(target.id, "gpt-5-mini", "85.0000", datetime(2026, 10, 1, 10, 0, tzinfo=UTC))

    admin = _admin_client()
    page = admin.get(f"/admin/customers/{target.id}?month=2026-09")
    assert "15.0000" in page.text  # за период
    assert "100.0000" in page.text  # за всё время


# ---------- выгрузка ----------


def test_csv_export_contains_the_period_numbers(client):
    _signup(client, "rep7@test.local", name="Пётр Выгрузкин")
    target = _customer("rep7@test.local")
    _make_event(target.id, "gpt-5-mini", "123.4500", datetime(2026, 11, 5, 10, 0, tzinfo=UTC))

    admin = _admin_client()
    r = admin.get("/admin/customers.csv?month=2026-11")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "flawless-2026-11.csv" in r.headers["content-disposition"]
    body = r.content.decode("utf-8")
    assert body.startswith("﻿")  # BOM, иначе русский Excel ломает кириллицу
    assert "Пётр Выгрузкин" in body
    assert "123,4500" in body  # запятая как десятичный разделитель для Excel


def test_csv_export_is_admin_only(client):
    _signup(client, "rep8@test.local")
    assert client.get("/admin/customers.csv").status_code == 403


def test_customer_detail_is_admin_only(client):
    _signup(client, "rep9@test.local")
    victim = _customer("rep9@test.local")
    attacker = _new_client()
    _signup(attacker, "rep10@test.local")
    assert attacker.get(f"/admin/customers/{victim.id}").status_code == 403
