"""Автотесты на закрытие входа в сервис (пункт 1 плана по итогам аудита
2026-09-07): регистрация по приглашению, отключение аккаунта админом,
проверка стойкости ключа подписи сессий.

conftest выставляет SIGNUP_MODE=open, чтобы остальные ~90 тестов могли
свободно регистрировать клиентов; закрытые режимы здесь включаются точечно
подменой settings.signup_mode. Хелперы продублированы из других test_*.py
намеренно (см. обоснование в test_features_wave2.py)."""

import asyncio
import re

import pytest
from sqlalchemy import select

from app.config import session_secret_is_weak, settings
from app.db import SessionLocal
from app.models import Customer, InviteCode

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123", **extra):
    return client.post(
        "/signup",
        data={"email": email, "name": name, "password": password, **extra},
        follow_redirects=True,
    )


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


def _issue_api_key(client):
    r = client.post("/api-key/regenerate", data={"name": "test"})
    assert r.status_code == 200
    m = re.search(r"nh_[A-Za-z0-9_-]+", r.text)
    assert m
    return m.group(0)


def _customer(email):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one_or_none()

    return asyncio.run(_get())


def _make_invite(code="test-invite-code", note=None):
    async def _create():
        async with SessionLocal() as session:
            admin = (
                await session.execute(select(Customer).where(Customer.email == ADMIN_EMAIL))
            ).scalar_one()
            session.add(InviteCode(code=code, note=note, created_by_admin_id=admin.id))
            await session.commit()

    asyncio.run(_create())
    return code


def _invite(code):
    async def _get():
        async with SessionLocal() as session:
            return (
                await session.execute(select(InviteCode).where(InviteCode.code == code))
            ).scalar_one()

    return asyncio.run(_get())


# ---------- стойкость ключа подписи сессий ----------


def test_known_placeholder_secrets_are_rejected_as_weak():
    # Именно эта пара ломала старую проверку: код сравнивал с "change-me",
    # а в .env.example лежало "change-me-session-secret".
    assert session_secret_is_weak("change-me")
    assert session_secret_is_weak("change-me-session-secret")
    assert session_secret_is_weak("  Change-Me  ")
    assert session_secret_is_weak("secret")


def test_short_secret_is_weak_and_long_random_is_not():
    assert session_secret_is_weak("a" * 31)
    assert not session_secret_is_weak("a" * 32)
    import secrets as _s

    assert not session_secret_is_weak(_s.token_urlsafe(48))


# ---------- режим invite ----------


def test_signup_without_invite_code_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "invite")
    r = _signup(client, "gate1@test.local")
    assert r.status_code == 400
    assert _customer("gate1@test.local") is None


def test_signup_with_unknown_invite_code_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "invite")
    r = _signup(client, "gate2@test.local", invite="no-such-code")
    assert r.status_code == 400
    assert _customer("gate2@test.local") is None


def test_signup_with_valid_invite_code_succeeds_and_burns_the_code(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "invite")
    code = _make_invite("gate3-code", note="Пётр, бэкенд")

    r = _signup(client, "gate3@test.local", invite=code)
    assert r.status_code == 200

    created = _customer("gate3@test.local")
    assert created is not None

    used = _invite(code)
    assert used.used_by_customer_id == created.id
    assert used.used_at is not None


def test_used_invite_code_cannot_be_reused(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "invite")
    code = _make_invite("gate4-code")

    assert _signup(client, "gate4a@test.local", invite=code).status_code == 200

    second = _new_client()
    r = _signup(second, "gate4b@test.local", invite=code)
    assert r.status_code == 400
    assert _customer("gate4b@test.local") is None


def test_invite_code_is_not_required_in_open_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "open")
    assert _signup(client, "gate5@test.local").status_code == 200
    assert _customer("gate5@test.local") is not None


def test_signup_is_refused_entirely_in_closed_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "closed")
    assert client.get("/signup").status_code == 403
    r = _signup(client, "gate6@test.local", invite="whatever")
    assert r.status_code == 403
    assert _customer("gate6@test.local") is None


# ---------- админ: приглашения ----------


def test_admin_can_generate_invite_code_and_see_it(client):
    admin = _admin_client()
    r = admin.post("/admin/invites/new", data={"note": "Новый разработчик"}, follow_redirects=True)
    assert r.status_code == 200
    assert "Новый разработчик" in r.text


def test_non_admin_cannot_reach_invites(client):
    _signup(client, "gate7@test.local")
    assert client.get("/admin/invites").status_code == 403
    assert client.post("/admin/invites/new", data={"note": "x"}).status_code == 403


# ---------- админ: отключение аккаунта ----------


def test_disabling_customer_kills_both_session_and_api_key(client):
    _signup(client, "gate8@test.local")
    api_key = _issue_api_key(client)
    target = _customer("gate8@test.local")
    assert target.active is True

    # до отключения обе двери работают
    assert client.get("/").status_code == 200
    assert (
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code
        == 402  # нет денег, но ключ принят
    )

    admin = _admin_client()
    r = admin.post(f"/admin/customers/{target.id}/toggle-active", follow_redirects=True)
    assert r.status_code == 200
    assert _customer("gate8@test.local").active is False

    # сессия больше не пускает в кабинет — редирект на публичный лендинг
    dashboard = client.get("/")
    assert "Создать аккаунт" in dashboard.text or "Войти" in dashboard.text

    # ключ отвергается сразу же
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_disabled_customer_cannot_log_back_in(client):
    _signup(client, "gate9@test.local", password="GatePass123")
    target = _customer("gate9@test.local")
    admin = _admin_client()
    admin.post(f"/admin/customers/{target.id}/toggle-active")

    fresh = _new_client()
    r = fresh.post("/login", data={"email": "gate9@test.local", "password": "GatePass123"})
    assert r.status_code == 401


def test_disabled_customer_can_be_re_enabled(client):
    _signup(client, "gate10@test.local")
    target = _customer("gate10@test.local")
    admin = _admin_client()
    admin.post(f"/admin/customers/{target.id}/toggle-active")
    assert _customer("gate10@test.local").active is False
    admin.post(f"/admin/customers/{target.id}/toggle-active")
    assert _customer("gate10@test.local").active is True


def test_admin_cannot_disable_own_account(client):
    admin = _admin_client()
    me = _customer(ADMIN_EMAIL)
    r = admin.post(f"/admin/customers/{me.id}/toggle-active")
    assert r.status_code == 400
    assert _customer(ADMIN_EMAIL).active is True


def test_non_admin_cannot_disable_anyone(client):
    _signup(client, "gate11@test.local")
    victim = _customer("gate11@test.local")

    attacker = _new_client()
    _signup(attacker, "gate12@test.local")
    r = attacker.post(f"/admin/customers/{victim.id}/toggle-active")
    assert r.status_code == 403
    assert _customer("gate11@test.local").active is True
