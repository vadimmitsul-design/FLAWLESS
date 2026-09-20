# -*- coding: utf-8 -*-
"""«Захожу в документацию — шапка как у анонима, хотя я вошёл» (2026-09-21).

Сервер отдавал верную разметку (проверено тем же куки через curl), дефект
был в отсутствии Cache-Control: браузер иногда показывал старый ответ из
своего HTTP-кэша вместо нового запроса — ответ до входа против шапки после
входа. Ни одна страница здесь не годится для кэша (разметка каждой зависит
от сессии), поэтому проверяем заголовок широко, а не точечно.
"""

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


def test_docs_page_is_never_cached(client):
    resp = client.get("/docs")
    assert resp.headers.get("cache-control") == "no-store, private"


def test_login_page_is_never_cached(client):
    resp = client.get("/login")
    assert resp.headers.get("cache-control") == "no-store, private"


def test_dashboard_is_never_cached_signed_in():
    admin = _admin_client()
    resp = admin.get("/")
    assert resp.headers.get("cache-control") == "no-store, private"


def test_json_api_error_is_never_cached(client):
    """Даже безобидный на вид 404 от API не должен оседать в кэше — списки
    моделей и ошибки тоже зависят от Bearer-ключа."""
    resp = client.get("/v1/models")
    assert resp.headers.get("cache-control") == "no-store, private"


def test_csv_export_is_never_cached():
    admin = _admin_client()
    resp = admin.get("/admin/customers.csv")
    assert resp.headers.get("cache-control") == "no-store, private"
