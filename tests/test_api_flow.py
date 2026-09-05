"""Сквозные тесты через TestClient — формализуют то, что раньше проверялось
вручную curl'ом в разработческой сессии. Общая SQLite-БД на весь прогон
(см. conftest.py) — изоляция между тестами через уникальные email в каждом
тесте, а не через отдельную БД на тест (проще, быстрее, тестов немного).
"""

import re

from app import llm, ratelimit

# Дублирует значения из conftest.py намеренно — импортировать оттуда не
# стоит: pytest сам загружает conftest.py как модуль "conftest" (без
# __init__.py в tests/), а `from tests.conftest import ...` привело бы к
# повторному исполнению файла как отдельного модуля "tests.conftest".
ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _extract(pattern, text):
    m = re.search(pattern, text)
    assert m, f"pattern {pattern!r} not found in response"
    return m.group(1) if m.lastindex else m.group(0)


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup",
        data={"email": email, "name": name, "password": password},
        follow_redirects=True,
    )
    assert r.status_code == 200
    return r


def _issue_api_key(client):
    r = client.post("/api-key/regenerate")
    assert r.status_code == 200
    return _extract(r"nh_[A-Za-z0-9_-]+", r.text)


def _admin_client():
    from fastapi.testclient import TestClient
    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def test_signup_login_dashboard(client):
    _signup(client, "flow1@test.local")
    r = client.get("/")
    assert r.status_code == 200
    assert "0.00" in r.text  # свежий баланс

    client.post("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200  # анонимному показывается публичный лендинг
    assert "Flawless" in r.text


def test_duplicate_signup_rejected(client):
    _signup(client, "flow2@test.local")
    client.post("/logout")
    r = client.post(
        "/signup",
        data={"email": "flow2@test.local", "name": "Dup", "password": "AnotherPass123"},
    )
    assert r.status_code == 409


def test_wrong_password_rejected(client):
    _signup(client, "flow3@test.local", password="RightPass123")
    client.post("/logout")
    r = client.post("/login", data={"email": "flow3@test.local", "password": "WrongPass"})
    assert r.status_code == 401


def test_insufficient_balance_blocks_call(client):
    _signup(client, "flow4@test.local")
    api_key = _issue_api_key(client)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "mock_response": "x"},
    )
    assert r.status_code == 402
    assert r.json()["detail"]["error"]["type"] == "insufficient_quota"


def test_topup_then_successful_call_deducts_balance(client):
    _signup(client, "flow5@test.local")
    api_key = _issue_api_key(client)

    client.post("/topups/new", data={"amount_rub": "50", "note": "test"})

    admin = _admin_client()
    topups_html = admin.get("/admin/topups").text
    topup_id = _extract(r"admin/topups/(\d+)/confirm", topups_html)
    r = admin.post(f"/admin/topups/{topup_id}/confirm")
    assert r.status_code in (200, 303)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "hello there, this is a mock reply",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello there, this is a mock reply"

    dashboard = client.get("/").text
    assert "gpt-5-mini" in dashboard
    # Списание крошечное — на 2 знаках баланс визуально не меняется (100.00
    # остаётся "100.00"), поэтому проверяем точную сумму в таблице вызовов,
    # а не отображаемый баланс.
    assert re.search(r"\d+\.\d{4} ₽", dashboard)


def test_streaming_call_bills_from_final_chunk_usage(client):
    _signup(client, "flow6@test.local")
    api_key = _issue_api_key(client)
    client.post("/topups/new", data={"amount_rub": "50"})
    admin = _admin_client()
    topup_id = _extract(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    admin.post(f"/admin/topups/{topup_id}/confirm")

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "mock_response": "streamed mock reply",
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        chunks = list(r.iter_lines())

    assert any("[DONE]" in c for c in chunks)
    assert any('"chat.completion.chunk"' in c for c in chunks)

    dashboard = client.get("/").text
    assert "gpt-5-mini" in dashboard


def test_unknown_model_returns_404(client):
    _signup(client, "flow7@test.local")
    api_key = _issue_api_key(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404


def test_invalid_api_key_returns_401(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer nh_totally_bogus"},
        json={"model": "gpt-5-mini", "messages": []},
    )
    assert r.status_code == 401


def test_non_admin_blocked_from_admin_pages(client):
    _signup(client, "flow8@test.local")
    assert client.get("/admin/topups").status_code == 403
    assert client.get("/admin/overview").status_code == 403
    assert client.get("/admin/password-resets").status_code == 403


def test_admin_overview_accessible_to_admin():
    admin = _admin_client()
    r = admin.get("/admin/overview")
    assert r.status_code == 200
    assert "Касса" in r.text


def test_fallback_to_backup_model_on_provider_error(client, monkeypatch):
    _signup(client, "flow9@test.local")
    api_key = _issue_api_key(client)
    client.post("/topups/new", data={"amount_rub": "50"})
    admin = _admin_client()
    topup_id = _extract(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    admin.post(f"/admin/topups/{topup_id}/confirm")

    import litellm

    real_chat_completion = llm.chat_completion
    calls = []

    async def flaky_chat_completion(alias, messages, **kwargs):
        calls.append(alias)
        if alias == "gpt-5-mini":
            raise litellm.RateLimitError(message="simulated", llm_provider="openai", model="gpt-5-mini")
        return await real_chat_completion(alias, messages, **kwargs)

    monkeypatch.setattr(llm, "chat_completion", flaky_chat_completion)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "mock_response": "fallback ok"},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "fallback ok"
    assert calls == ["gpt-5-mini", "gemini-flash"]


def test_rate_limit_returns_429_after_threshold(client, monkeypatch):
    _signup(client, "flow10@test.local")
    api_key = _issue_api_key(client)
    monkeypatch.setattr(ratelimit.settings, "rate_limit_per_window", 3)

    statuses = []
    for _ in range(5):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "mock_response": "x"},
        )
        statuses.append(r.status_code)

    assert statuses[:3] == [402, 402, 402]  # баланс нулевой, но это уже ПОСЛЕ учёта лимита
    assert 429 in statuses


def test_forgot_password_admin_reset_flow(client):
    _signup(client, "flow11@test.local", password="OldPass123")
    client.post("/logout")

    r = client.post("/forgot-password", data={"email": "flow11@test.local"})
    assert r.status_code == 200
    assert "администратор" in r.text.lower() or "заявка" in r.text.lower() or "Если" in r.text

    admin = _admin_client()
    resets_html = admin.get("/admin/password-resets").text
    assert "flow11@test.local" in resets_html
    reset_id = _extract(r"admin/password-resets/(\d+)/reset", resets_html)

    r = admin.post(f"/admin/password-resets/{reset_id}/reset")
    assert r.status_code == 200
    new_password = _extract(r"<code[^>]*>([^<]+)</code>", r.text)

    r = client.post("/login", data={"email": "flow11@test.local", "password": "OldPass123"})
    assert r.status_code == 401

    r = client.post("/login", data={"email": "flow11@test.local", "password": new_password}, follow_redirects=True)
    assert r.status_code == 200


def test_forgot_password_does_not_leak_unknown_email(client):
    r = client.post("/forgot-password", data={"email": "no-such-customer@test.local"})
    assert r.status_code == 200
    admin = _admin_client()
    resets_html = admin.get("/admin/password-resets").text
    assert "no-such-customer@test.local" not in resets_html
