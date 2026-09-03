"""Автотесты на Telegram AI-секретаря (вручную полный цикл — привязка,
текст, fallback между провайдерами — проверен на реальном @rossi_AIhub_bot
2026-09-03, см. CLAUDE.md). Здесь то же самое формализовано в pytest:
внешние вызовы замоканы — send_message пишет в список вместо HTTP на
api.telegram.org, llm.chat_completion_with_fallback возвращает канонический
litellm-ответ вместо реального обращения к провайдеру (тот же приём, что
mock_response у /v1/chat/completions в test_features_wave2.py, только на
уровне функции — chatcore.run_chat_turn его не пробрасывает)."""

import asyncio

import pytest
from sqlalchemy import select

from app import billing, chatcore, llm, telegram_bot
from app.db import SessionLocal
from app.models import Customer, TelegramLink, TelegramLinkCode

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"

_FAKE_RESPONSE = {
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}},
    "choices": [{"message": {"content": "mocked reply"}}],
}


async def _fake_fallback(alias, messages, **kwargs):
    return alias, "openai", "gpt-5-mini", _FAKE_RESPONSE


def _run(coro):
    return asyncio.run(coro)


def _signup(client, email, name="TG User", password="TestPass123"):
    r = client.post("/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True)
    assert r.status_code == 200


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


def _customer_id(email):
    async def _get():
        async with SessionLocal() as session:
            customer = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one()
            return customer.id

    return _run(_get())


def _link_code(client):
    r = client.post("/telegram/link", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("telegram_code=")[1]


# ---------- chatcore.run_chat_turn ----------


def test_run_chat_turn_bills_and_returns_reply(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_fallback)
    _signup(client, "chatcore1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    customer_id = _customer_id("chatcore1@test.local")

    async def _call():
        async with SessionLocal() as session:
            return await chatcore.run_chat_turn(
                session, customer_id, "gpt-5-mini", [{"role": "user", "content": "hi"}]
            )

    reply = _run(_call())
    assert reply == "mocked reply"

    dashboard = client.get("/").text
    assert "gpt-5-mini" in dashboard
    assert "ок" in dashboard  # статус success в таблице "Последние вызовы"


def test_run_chat_turn_insufficient_balance_raises(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_fallback)
    _signup(client, "chatcore2@test.local")
    customer_id = _customer_id("chatcore2@test.local")

    async def _call():
        async with SessionLocal() as session:
            return await chatcore.run_chat_turn(
                session, customer_id, "gpt-5-mini", [{"role": "user", "content": "hi"}]
            )

    with pytest.raises(billing.InsufficientBalance):
        _run(_call())


# ---------- telegram_bot: привязка аккаунта ----------


def test_telegram_start_links_account(client, monkeypatch):
    sent = []

    async def _fake_send(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    _signup(client, "tguser1@test.local")
    code = _link_code(client)
    customer_id = _customer_id("tguser1@test.local")

    _run(telegram_bot._handle_update({"message": {"chat": {"id": 555111}, "text": f"/start {code}"}}))

    assert sent and "Готово" in sent[-1][1]

    async def _check():
        async with SessionLocal() as session:
            link = (
                await session.execute(select(TelegramLink).where(TelegramLink.chat_id == 555111))
            ).scalar_one()
            remaining = (
                await session.execute(select(TelegramLinkCode).where(TelegramLinkCode.code == code))
            ).scalars().all()
            return link, remaining

    link, remaining = _run(_check())
    assert link.customer_id == customer_id
    assert remaining == []  # код одноразовый, удалён сразу после использования


def test_telegram_start_with_unknown_code_rejected():
    sent = []

    async def _fake_send(chat_id, text):
        sent.append((chat_id, text))

    import app.telegram_bot as tb_module

    orig = tb_module.send_message
    tb_module.send_message = _fake_send
    try:
        _run(tb_module._handle_update({"message": {"chat": {"id": 999888}, "text": "/start does-not-exist"}}))
    finally:
        tb_module.send_message = orig

    assert sent and "не найден" in sent[-1][1].lower()

    async def _check():
        async with SessionLocal() as session:
            return (
                await session.execute(select(TelegramLink).where(TelegramLink.chat_id == 999888))
            ).scalar_one_or_none()

    assert _run(_check()) is None


def test_telegram_message_from_unlinked_chat_prompts_link(monkeypatch):
    sent = []

    async def _fake_send(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    _run(telegram_bot._handle_update({"message": {"chat": {"id": 111000}, "text": "hello"}}))
    assert sent and "не подключён" in sent[-1][1].lower()


# ---------- telegram_bot: обмен сообщениями (полный путь через chatcore + billing) ----------


def test_telegram_text_message_bills_and_replies(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_fallback)
    sent = []

    async def _fake_send(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    _signup(client, "tguser2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    code = _link_code(client)
    _run(telegram_bot._handle_update({"message": {"chat": {"id": 777222}, "text": f"/start {code}"}}))
    sent.clear()

    _run(telegram_bot._handle_update({"message": {"chat": {"id": 777222}, "text": "Привет, как дела?"}}))

    assert sent == [(777222, "mocked reply")]  # без префикса — префикс только у голосовых

    dashboard = client.get("/").text
    assert "Аккаунт подключён" in dashboard


def test_telegram_text_message_insufficient_balance_replies_with_notice(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_fallback)
    sent = []

    async def _fake_send(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    _signup(client, "tguser3@test.local")  # без пополнения — баланс 0
    code = _link_code(client)
    _run(telegram_bot._handle_update({"message": {"chat": {"id": 333444}, "text": f"/start {code}"}}))
    sent.clear()

    _run(telegram_bot._handle_update({"message": {"chat": {"id": 333444}, "text": "hello"}}))
    assert sent and "Недостаточно средств" in sent[-1][1]
