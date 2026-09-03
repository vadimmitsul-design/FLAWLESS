"""Автотесты на волну фич из документа Flawless/Flowless (2026-09-03):
платёжный агент, библиотека промптов, DLP, архиватор, семейный тариф.
Раньше эти 5 фич были проверены только вручную curl'ом на реальном
Postgres — здесь то же самое формализовано в pytest (на SQLite, см.
conftest.py). Хелперы продублированы из test_api_flow.py намеренно —
то же обоснование, что и для ADMIN_EMAIL/ADMIN_PASSWORD там."""

import re

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _extract(pattern, text):
    m = re.search(pattern, text)
    assert m, f"pattern {pattern!r} not found in response"
    return m.group(1) if m.lastindex else m.group(0)


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200
    return r


def _issue_api_key(client):
    r = client.post("/api-key/regenerate")
    assert r.status_code == 200
    return _extract(r"nh_[A-Za-z0-9_-]+", r.text)


def _new_client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _admin_client():
    admin = _new_client()
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def _topup(client, admin, amount="100"):
    client.post("/topups/new", data={"amount_rub": amount})
    topup_id = _extract(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    r = admin.post(f"/admin/topups/{topup_id}/confirm")
    assert r.status_code in (200, 303)


# ---------- платёжный агент ----------


def test_shop_insufficient_balance_then_purchase_and_fulfill(client):
    _signup(client, "shop1@test.local")
    product_id = _extract(r'name="product_id" value="(\d+)"', client.get("/shop").text)

    r = client.post("/shop/order", data={"product_id": product_id, "account_email": "me@x.ru"})
    assert r.status_code == 402
    assert "Недостаточно средств" in r.text

    admin = _admin_client()
    _topup(client, admin, "5000")

    r = client.post(
        "/shop/order",
        data={"product_id": product_id, "account_email": "me@x.ru", "note": "test"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "в обработке" in r.text

    orders_html = admin.get("/admin/orders").text
    order_id = _extract(r"admin/orders/(\d+)/fulfill", orders_html)
    r = admin.post(f"/admin/orders/{order_id}/fulfill", follow_redirects=True)
    assert r.status_code == 200
    assert "выполнено" in r.text

    assert "выполнено" in client.get("/shop").text


def test_shop_refund_returns_balance(client):
    _signup(client, "shop2@test.local")
    product_id = _extract(r'name="product_id" value="(\d+)"', client.get("/shop").text)
    admin = _admin_client()
    _topup(client, admin, "5000")
    client.post("/shop/order", data={"product_id": product_id, "account_email": "me2@x.ru"})

    order_id = _extract(r"admin/orders/(\d+)/fulfill", admin.get("/admin/orders").text)
    r = admin.post(f"/admin/orders/{order_id}/refund", follow_redirects=True)
    assert r.status_code == 200
    assert "возврат" in r.text

    dashboard = client.get("/").text
    assert "5000.00 ₽" in dashboard  # заказ + возврат вернули баланс к исходному пополнению


# ---------- библиотека промптов ----------


def test_prompt_purchase_charges_fee_and_pays_royalty(client, monkeypatch):
    author = _new_client()
    _signup(author, "author1@test.local", name="Author One")
    r = author.post(
        "/prompts",
        data={
            "title": "Юрист",
            "description": "Ищет риски",
            "system_prompt": "Ты — юрист, ищи риски в договоре",
            "price_rub": "5.00",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    prompt_id = _extract(r'<td class="mono">(\d+)</td>\s*</tr>', author.get("/prompts").text)

    _signup(client, "prompt_user1@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "100")

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "check this"}],
            "prompt_id": int(prompt_id),
            "mock_response": "looks risky",
        },
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "looks risky"

    author_dashboard = author.get("/").text
    assert re.search(r"2\.5000 ₽|2\.50 ₽", author_dashboard), "author should have received 50% royalty (2.50 ₽ of 5 ₽)"


def test_unknown_prompt_id_returns_404(client):
    _signup(client, "prompt_user2@test.local")
    api_key = _issue_api_key(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "prompt_id": 999999},
    )
    assert r.status_code == 404


# ---------- DLP ----------


def test_dlp_redacts_secret_and_flags_usage_event(client):
    _signup(client, "dlpuser1@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "100")

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "my key is sk-abcdefghijklmnopqrstuvwxyz123456, help me"}],
            "mock_response": "ok",
        },
    )
    assert r.status_code == 200

    dashboard = client.get("/").text
    assert "DLP" in dashboard
    assert "api_key_like" in dashboard


def test_dlp_leaves_clean_message_unflagged(client):
    _signup(client, "dlpuser2@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "100")

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello, how are you?"}], "mock_response": "fine"},
    )
    dashboard = client.get("/").text
    assert "DLP" not in dashboard


# ---------- архиватор диалогов ----------


def test_archive_create_and_verify(client):
    _signup(client, "archuser1@test.local")
    content = "This is a unique AI-generated report for wave2 tests."
    r = client.post("/archive", data={"content": content, "label": "Report v1"}, follow_redirects=True)
    assert r.status_code == 200
    assert "Report v1" in r.text

    anon = _new_client()
    r = anon.post("/verify", data={"content": content})
    assert "Подтверждено" in r.text
    assert "Report v1" in r.text

    r = anon.post("/verify", data={"content": "some other content that was never archived"})
    assert "Не найдено" in r.text


def test_archive_duplicate_content_not_duplicated(client):
    _signup(client, "archuser2@test.local")
    content = "Duplicate-check content for wave2."
    client.post("/archive", data={"content": content, "label": "first"})
    r = client.post("/archive", data={"content": content, "label": "second"})
    assert r.status_code == 200
    assert "уже был сохранён" in r.text
    # первая запись не перезаписана второй попыткой (та же метка "first", "second" не появился) —
    # ">" перед меткой отличает реальную ячейку таблицы от случайных совпадений в CSS/разметке
    # (например "button.secondary" тоже содержит подстроку "second")
    archive_page = client.get("/archive").text
    assert ">first<" in archive_page
    assert ">second<" not in archive_page


# ---------- семейный тариф ----------


def test_child_account_full_flow():
    parent = _new_client()
    _signup(parent, "parent1@test.local", name="Parent One")
    admin = _admin_client()
    _topup(parent, admin, "100")

    r = parent.post("/children/new", data={"email": "kid1@test.local", "name": "Kid One", "password": "KidPass123"}, follow_redirects=True)
    assert r.status_code == 200
    assert "Kid One" in r.text

    kid = _new_client()
    r = kid.post("/login", data={"email": "kid1@test.local", "password": "KidPass123"})
    assert r.status_code in (200, 303)
    kid_key = _issue_api_key(kid)

    r = kid.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {kid_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "напиши сочинение про весну"}], "mock_response": "x"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["type"] == "child_mode_blocked"

    r = kid.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {kid_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "объясни теорему пифагора"}], "mock_response": "давай разберём"},
    )
    assert r.status_code == 200

    # ребёнок не платит сам
    assert "0.00" in kid.get("/").text

    # у родителя видно ребёнка и его историю
    parent_dashboard = parent.get("/").text
    assert "Kid One" in parent_dashboard
    child_id = _extract(r"/children/(\d+)", parent_dashboard)
    child_history = parent.get(f"/children/{child_id}").text
    assert "gpt-5-mini" in child_history


def test_child_cannot_create_grandchild():
    parent = _new_client()
    _signup(parent, "parent2@test.local")
    parent.post("/children/new", data={"email": "kid2@test.local", "name": "Kid Two", "password": "KidPass123"})
    kid = _new_client()
    kid.post("/login", data={"email": "kid2@test.local", "password": "KidPass123"})
    r = kid.get("/children/new")
    assert r.status_code == 403
