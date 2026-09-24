"""Автотесты на управление API-ключами и лимиты расхода (2.1-2.2 доработок):
несколько именованных ключей на клиента, отзыв, дневной/месячный потолок
расхода на ключ (свой + потолок админа поверх), rate limit по ключу, а не
по клиенту. Хелперы продублированы из других test_*.py намеренно (см.
обоснование в test_features_wave2.py)."""

import re

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _issue_api_key(client, name=""):
    r = client.post("/api-key/regenerate", data={"name": name})
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
    r = admin.post(f"/admin/topups/{m.group(1)}/confirm")
    assert r.status_code in (200, 303)


def _key_id_from_dashboard(client, name):
    """Кабинет не отдаёт id ключа напрямую в разметке — вытаскиваем из
    action= формы отзыва рядом с нужным названием (единственное место, где
    id ключа виден клиенту)."""
    html = client.get("/").text
    idx = html.index(f">{name}<") if f">{name}<" in html else html.index(name)
    m = re.search(r"/api-keys/(\d+)/revoke", html[idx:])
    assert m
    return int(m.group(1))


# ---------- 2.1 несколько именованных ключей ----------


def test_multiple_named_keys_all_independently_valid(client):
    _signup(client, "multikey1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    key_a = _issue_api_key(client, "Сервер A")
    key_b = _issue_api_key(client, "Сервер B")
    assert key_a != key_b

    for key in (key_a, key_b):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-5-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "mock_response": "ok",
            },
        )
        assert r.status_code == 200

    dashboard = client.get("/").text
    assert "Сервер A" in dashboard
    assert "Сервер B" in dashboard


def test_revoking_one_key_does_not_affect_another(client):
    _signup(client, "multikey2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    key_a = _issue_api_key(client, "Ключ A")
    key_b = _issue_api_key(client, "Ключ B")

    key_a_id = _key_id_from_dashboard(client, "Ключ A")
    r = client.post(f"/api-keys/{key_a_id}/revoke")
    assert r.status_code in (200, 303)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_a}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "x",
        },
    )
    assert r.status_code == 401

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_b}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "still works",
        },
    )
    assert r.status_code == 200


def test_key_hash_not_reversible_only_last_four_shown(client):
    _signup(client, "multikey3@test.local")
    raw_key = _issue_api_key(client, "Мой ключ")
    dashboard = client.get("/").text
    assert raw_key not in dashboard
    assert f"…{raw_key[-4:]}" in dashboard


def test_cannot_revoke_someone_elses_key(client):
    _signup(client, "victim@test.local")
    _issue_api_key(client, "Victim key")
    victim_key_id = _key_id_from_dashboard(client, "Victim key")

    from fastapi.testclient import TestClient

    from app.main import app

    attacker = TestClient(app)
    _signup(attacker, "attacker@test.local")
    r = attacker.post(f"/api-keys/{victim_key_id}/revoke")
    assert r.status_code == 404


# ---------- 2.2 лимиты расхода ----------


def test_client_daily_limit_blocks_further_calls_same_day(client):
    _signup(client, "limit1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    api_key = _issue_api_key(client, "Лимитный")
    key_id = _key_id_from_dashboard(client, "Лимитный")

    r = client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "0.001", "monthly_limit_rub": ""}
    )
    assert r.status_code in (200, 303)

    r1 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "first call goes through",
        },
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi again"}],
            "mock_response": "blocked",
        },
    )
    assert r2.status_code == 429
    assert r2.json()["detail"]["error"]["type"] == "spend_limit_exceeded"


def test_admin_limit_caps_even_when_client_limit_is_higher(client):
    _signup(client, "limit2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    api_key = _issue_api_key(client, "Под колпаком")
    key_id = _key_id_from_dashboard(client, "Под колпаком")

    client.post(
        f"/api-keys/{key_id}/limits", data={"daily_limit_rub": "1000", "monthly_limit_rub": ""}
    )
    r = admin.post(
        f"/admin/api-keys/{key_id}/limits",
        data={"daily_limit_rub": "0.001", "monthly_limit_rub": ""},
    )
    assert r.status_code in (200, 303)

    r1 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "goes through once",
        },
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "blocked by admin ceiling",
        },
    )
    assert r2.status_code == 429


def test_rate_limit_is_per_key_not_per_customer(client):
    """Раньше rate limit считался по customer_id — теперь по ключу, чтобы
    у клиента с несколькими интеграциями одна не забивала лимит другой."""
    _signup(client, "ratelimit1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    key_a = _issue_api_key(client, "A")
    key_b = _issue_api_key(client, "B")

    from app.core.config import settings

    for _ in range(settings.rate_limit_per_window):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key_a}"},
            json={
                "model": "gpt-5-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "mock_response": "x",
            },
        )
        assert r.status_code == 200

    r_blocked = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_a}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "x",
        },
    )
    assert r_blocked.status_code == 429

    # ключ B ещё не трогали — его собственный лимит не исчерпан
    r_other_key = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_b}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "still fine",
        },
    )
    assert r_other_key.status_code == 200
