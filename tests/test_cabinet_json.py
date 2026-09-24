"""Real session, ownership and monetary contracts of the Next.js adapter."""

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db import SessionLocal
from app.db.models import Customer, UsageEvent, WebConversation, WebMessage, utcnow

PASSWORD = "CabinetTest123!"


def _account(client: TestClient) -> dict:
    email = f"cabinet-{uuid.uuid4().hex}@test.local"
    response = client.post(
        "/signup", data={"email": email, "name": "Cabinet customer", "password": PASSWORD}
    )
    assert response.status_code == 200
    return client.get("/cabinet-api/session").json()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/session", None),
        ("GET", "/dashboard", None),
        ("GET", "/conversations/1", None),
        ("POST", "/keys", {"name": "private"}),
        ("DELETE", "/keys/1", None),
        ("POST", "/topups", {"amount_rub": "100"}),
    ],
)
def test_json_cabinet_requires_session(client, method, path, payload):
    response = client.request(method, "/cabinet-api" + path, json=payload)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_json_login_logout_and_invalid_credentials(client):
    customer = _account(client)
    assert client.post("/cabinet-api/logout").json() == {"ok": True}
    assert client.get("/cabinet-api/session").status_code == 401
    assert (
        client.post(
            "/cabinet-api/login", json={"email": customer["email"], "password": "wrong"}
        ).status_code
        == 401
    )
    response = client.post(
        "/cabinet-api/login", json={"email": customer["email"].upper(), "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json() == customer
    assert client.get("/cabinet-api/session").json() == customer
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "password_hash" not in response.text


def test_json_login_shares_existing_bruteforce_limit(client):
    for _ in range(10):
        assert (
            client.post(
                "/cabinet-api/login", json={"email": "absent@test.local", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post("/login", data={"email": "absent@test.local", "password": "wrong"}).status_code
        == 429
    )


def test_keys_are_once_only_and_owned(client):
    owner = _account(client)
    response = client.post("/cabinet-api/keys", json={"name": "  Deploy  "})
    assert response.status_code == 201
    raw = response.json()["raw_key"]
    dashboard = client.get("/cabinet-api/dashboard")
    key = dashboard.json()["api_keys"][0]
    assert key["name"] == "Deploy"
    assert key["prefix"].endswith(raw[-4:])
    assert raw not in dashboard.text
    assert "key_hash" not in dashboard.text
    assert "password_hash" not in dashboard.text
    client.post("/cabinet-api/logout")
    _account(client)
    assert client.delete(f"/cabinet-api/keys/{key['id']}").status_code == 404
    assert client.get("/cabinet-api/dashboard").json()["api_keys"] == []
    client.post("/cabinet-api/login", json={"email": owner["email"], "password": PASSWORD})
    assert client.delete(f"/cabinet-api/keys/{key['id']}").status_code == 200
    assert client.get("/cabinet-api/dashboard").json()["api_keys"] == []


def test_topup_is_a_request_and_rejects_cross_origin(client):
    _account(client)
    response = client.post("/cabinet-api/topups", json={"amount_rub": "123.45", "note": "Invoice"})
    assert response.status_code == 201
    data = client.get("/cabinet-api/dashboard").json()
    assert Decimal(data["wallet"]["balance_rub"]) == 0
    assert data["topups"][0]["amount_rub"] == "123.45"
    assert data["topups"][0]["status"] == "requested"
    assert (
        client.post(
            "/cabinet-api/topups",
            json={"amount_rub": "999"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.delete("/cabinet-api/keys/1", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        client.post(
            "/cabinet-api/login",
            json={"email": "irrelevant", "password": "irrelevant"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "1.001", "1000000000000"])
def test_topup_rejects_invalid_money(client, amount):
    _account(client)
    assert client.post("/cabinet-api/topups", json={"amount_rub": amount}).status_code == 422


def test_dashboard_and_conversations_are_customer_scoped(client):
    owner = _account(client)

    async def seed() -> int:
        async with SessionLocal() as session:
            customer = await session.get(Customer, owner["id"])
            customer.balance_rub = Decimal("100.1234")
            history = WebConversation(
                customer_id=customer.id, title="Private conversation", model_alias="gpt-5-mini"
            )
            session.add(history)
            await session.flush()
            session.add(
                WebMessage(
                    conversation_id=history.id,
                    role="user",
                    content=json.dumps(
                        [
                            {"type": "text", "text": "Image question"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,secret"},
                            },
                        ]
                    ),
                    attachment_name="diagram.png",
                )
            )
            session.add(
                UsageEvent(
                    customer_id=customer.id,
                    billing_customer_id=customer.id,
                    provider="openrouter",
                    model="openai/gpt-5-mini",
                    input_tokens=120,
                    output_tokens=80,
                    charged_rub=Decimal("3.1234"),
                    status="success",
                    created_at=utcnow(),
                )
            )
            session.add(
                UsageEvent(
                    customer_id=customer.id,
                    billing_customer_id=customer.id,
                    provider="openrouter",
                    model="openai/gpt-5-mini",
                    reserved_rub=Decimal("5.2345"),
                    status="pending",
                    created_at=utcnow(),
                )
            )
            await session.commit()
            return history.id

    conversation_id = asyncio.run(seed())
    data = client.get("/cabinet-api/dashboard").json()
    assert data["wallet"]["balance_rub"] == "100.1234"
    assert data["wallet"]["reserved_rub"] == "5.2345"
    assert data["wallet"]["available_rub"] == "94.8889"
    assert data["wallet"]["spent_month_rub"] == "3.1234"
    assert data["stats"] == {"requests_month": 2, "tokens_month": 200}
    assert len(data["daily_usage"]) == 7
    assert data["daily_usage"][-1]["charged_rub"] == "3.1234"
    assert len(data["usage"]) == 2
    assert data["conversations"][0]["id"] == conversation_id
    response = client.get(f"/cabinet-api/conversations/{conversation_id}")
    assert response.json()["messages"] == [
        {"role": "user", "text": "Image question", "attachment_name": "diagram.png"}
    ]
    assert "base64" not in response.text
    client.post("/cabinet-api/logout")
    _account(client)
    assert client.get(f"/cabinet-api/conversations/{conversation_id}").status_code == 404
    stranger = client.get("/cabinet-api/dashboard").json()
    assert stranger["usage"] == []
    assert stranger["conversations"] == []
    assert stranger["stats"] == {"requests_month": 0, "tokens_month": 0}


def test_child_uses_parent_wallet_but_own_history(client):
    parent = _account(client)
    child_email = f"child-{uuid.uuid4().hex}@test.local"

    async def seed() -> None:
        async with SessionLocal() as session:
            payer = await session.get(Customer, parent["id"])
            payer.balance_rub = Decimal("500.00")
            payer.daily_limit_rub = Decimal("30.00")
            session.add(
                Customer(
                    name="Child",
                    email=child_email,
                    password_hash=hash_password(PASSWORD),
                    is_child=True,
                    parent_customer_id=payer.id,
                )
            )
            await session.commit()

    asyncio.run(seed())
    client.post("/cabinet-api/login", json={"email": child_email, "password": PASSWORD})
    data = client.get("/cabinet-api/dashboard").json()
    assert data["customer"]["is_child"] is True
    assert data["flags"]["children"] is False
    assert Decimal(data["wallet"]["balance_rub"]) == Decimal("500.00")
    assert Decimal(data["wallet"]["daily_limit_rub"]) == Decimal("30.00")
    assert client.post("/cabinet-api/topups", json={"amount_rub": "100"}).status_code == 403
