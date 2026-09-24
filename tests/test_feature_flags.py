"""Автотесты на отключаемые разделы — то, чем внутренний экземпляр сервиса
отличается от клиентского (решение 2026-09-08: два экземпляра на одном коде,
раздельные базы и настройки).

Ключевое требование: выключенный раздел должен не только исчезать из меню,
но и ОТДАВАТЬ 404 по своим адресам. Иначе выключение косметическое — раздел
продолжает работать в обход интерфейса, а именно в магазине, платных
промптах и детских аккаунтах живёт большая часть находок аудита.

Хелперы продублированы намеренно (см. test_features_wave2.py)."""

import asyncio
import re
from decimal import Decimal

from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.db.models import Customer, Product, Prompt

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


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one()

    return asyncio.run(_get())


def _product_id():
    async def _get():
        async with SessionLocal() as session:
            return (await session.execute(select(Product))).scalars().first().id

    return asyncio.run(_get())


# ---------- магазин ----------


def test_shop_routes_return_404_when_disabled(client, monkeypatch):
    _signup(client, "flag_shop@test.local")
    monkeypatch.setattr(settings, "enable_shop", False)

    assert client.get("/shop").status_code == 404
    assert (
        client.post(
            "/shop/order",
            data={"product_id": _product_id(), "account_email": "x@test.local", "note": ""},
        ).status_code
        == 404
    )

    admin = _admin_client()
    assert admin.get("/admin/orders").status_code == 404


def test_shop_disappears_from_the_menu_when_disabled(client, monkeypatch):
    _signup(client, "flag_shop2@test.local")
    assert "/shop" in client.get("/").text

    monkeypatch.setattr(settings, "enable_shop", False)
    assert "/shop" not in client.get("/").text


# ---------- промпты ----------


def test_prompt_routes_return_404_when_disabled(client, monkeypatch):
    _signup(client, "flag_prompts@test.local")
    monkeypatch.setattr(settings, "enable_prompts", False)

    assert client.get("/prompts").status_code == 404
    assert (
        client.post(
            "/prompts",
            data={"title": "x", "description": "", "system_prompt": "y", "price_rub": "10"},
        ).status_code
        == 404
    )


def test_prompt_id_is_rejected_through_the_api_when_prompts_are_disabled(client, monkeypatch):
    """Выключение раздела должно закрывать и подстановку промпта в API —
    иначе выключён только интерфейс, а платная механика продолжает работать."""
    _signup(client, "flag_prompts2@test.local")
    target = _customer("flag_prompts2@test.local")
    admin = _admin_client()
    admin.post(
        f"/admin/customers/{target.id}/balance",
        data={"amount_rub": "1000", "entry_type": "adjustment", "note": "бюджет"},
    )

    async def _make_prompt():
        async with SessionLocal() as session:
            prompt = Prompt(
                author_customer_id=target.id,
                title="Промпт",
                system_prompt="Ты помощник",
                price_rub=Decimal("50"),
            )
            session.add(prompt)
            await session.commit()
            return prompt.id

    prompt_id = asyncio.run(_make_prompt())
    api_key = re.search(
        r"nh_[A-Za-z0-9_-]+", client.post("/api-key/regenerate", data={"name": "k"}).text
    ).group(0)

    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "prompt_id": prompt_id,
        "mock_response": "ok",
    }

    # включено — промпт находится
    assert (
        client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload
        ).status_code
        == 200
    )

    monkeypatch.setattr(settings, "enable_prompts", False)
    r = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload
    )
    assert r.status_code == 404


# ---------- детские аккаунты ----------


def test_children_routes_return_404_when_disabled(client, monkeypatch):
    _signup(client, "flag_kids@test.local")
    monkeypatch.setattr(settings, "enable_children", False)

    assert client.get("/children/new").status_code == 404
    assert (
        client.post(
            "/children/new",
            data={"email": "flag_kid@test.local", "name": "К", "password": "KidPass123"},
        ).status_code
        == 404
    )
    assert client.get("/children/1").status_code == 404


def test_children_section_hidden_from_the_dashboard(client, monkeypatch):
    _signup(client, "flag_kids2@test.local")
    assert "Детские аккаунты" in client.get("/").text

    monkeypatch.setattr(settings, "enable_children", False)
    assert "Детские аккаунты" not in client.get("/").text


# ---------- архив ----------


def test_archive_routes_return_404_when_disabled(client, monkeypatch):
    _signup(client, "flag_arch@test.local")
    monkeypatch.setattr(settings, "enable_archive", False)

    assert client.get("/archive").status_code == 404
    assert client.get("/verify").status_code == 404


# ---------- всё включено по умолчанию ----------


def test_everything_is_available_by_default(client):
    """Клиентский экземпляр ничего не выключает — проверяем, что флаги не
    сломали обычную работу."""
    _signup(client, "flag_all@test.local")
    assert client.get("/shop").status_code == 200
    assert client.get("/prompts").status_code == 200
    assert client.get("/archive").status_code == 200
    assert client.get("/children/new").status_code == 200


# ---------- подпись экземпляра ----------


def test_instance_name_is_shown_in_the_header(client, monkeypatch):
    """Две одинаковые админки рядом легко перепутать."""
    _signup(client, "flag_name@test.local")
    assert "внутренний" not in client.get("/").text

    monkeypatch.setattr(settings, "instance_name", "внутренний")
    assert "внутренний" in client.get("/").text


def test_templates_cannot_reach_secrets_through_the_flags_object():
    """В шаблоны отдаётся только набор флагов, а не весь конфиг — иначе
    разметка могла бы отрендерить session_secret или ключи провайдеров."""
    from app.main import app

    flags = app.state.templates.env.globals["features"]
    assert not hasattr(flags, "session_secret")
    assert not hasattr(flags, "database_url")
    assert flags.enable_shop == settings.enable_shop
