"""Заявка на ресурс в долларах показывалась со знаком рубля (2026-09-21).

Двойной дефект: у формы `/resources/request` не было поля валюты вообще
(сервер всегда писал RUB, что бы человек ни имел в виду), а очередь
администратора для суммы заявки хардкодила «₽» вместо того, чтобы читать
поле currency. Нашлось на живых данных: заявка Хасаи М.В. на Claude Max 5x
($116) в очереди показывала «116,00 ₽».

Префикс — `curr21_`.
"""

import asyncio

from app.db import SessionLocal
from app.db.models import Customer

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


def _employee(email):
    from app.core.security import hash_password

    async def _add():
        async with SessionLocal() as session:
            person = Customer(
                email=email, name="Заявитель", password_hash=hash_password("Passw0rd!")
            )
            session.add(person)
            await session.commit()
            return person.id

    return asyncio.run(_add())


def test_employee_can_request_in_dollars_not_only_rubles(client):
    """Раньше поля валюты не было вовсе — заявка на доллары физически
    не отправлялась, сервер всегда писал RUB."""
    email = "curr21_employee@test.local"
    _employee(email)
    assert client.post("/login", data={"email": email, "password": "Passw0rd!"}).status_code in (
        200,
        303,
    )

    resp = client.post(
        "/resources/request",
        data={
            "kind": "subscription",
            "name": "Claude Max 5x",
            "estimated_amount": "116",
            "currency": "USD",
        },
    )
    assert resp.status_code in (200, 303)

    from sqlalchemy import select

    from app.db.models import ResourceRequest

    async def _read():
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(ResourceRequest).where(ResourceRequest.name == "Claude Max 5x")
                )
            ).scalar_one()

    req = asyncio.run(_read())
    assert req.currency == "USD"


def test_unknown_currency_is_rejected_not_silently_stored_as_rub(client):
    email = "curr21_badcur@test.local"
    _employee(email)
    assert client.post("/login", data={"email": email, "password": "Passw0rd!"}).status_code in (
        200,
        303,
    )
    resp = client.post(
        "/resources/request",
        data={"kind": "subscription", "name": "Что-то", "currency": "GBP"},
    )
    assert resp.status_code == 400


def test_pending_queue_shows_dollar_sign_not_ruble_for_a_dollar_request(client):
    """Сумма верная, знак валюты — нет: было «116,00 ₽» для заявки в
    долларах. Проверяем именно то, что видит администратор в очереди."""
    email = "curr21_queue@test.local"
    _employee(email)
    assert client.post("/login", data={"email": email, "password": "Passw0rd!"}).status_code in (
        200,
        303,
    )
    assert client.post(
        "/resources/request",
        data={
            "kind": "subscription",
            "name": "Дашбордный доллар",
            "estimated_amount": "50",
            "currency": "USD",
        },
    ).status_code in (200, 303)

    page = _admin_client().get("/admin/resources").text
    # &nbsp; в исходнике HTML остаётся буквальным текстом — браузер, а не
    # httpx, превращает его в неразрывный пробел при отрисовке.
    assert "50,00&nbsp;$" in page
    assert "50,00&nbsp;₽" not in page
