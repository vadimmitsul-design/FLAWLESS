"""Автотесты на веб-чат в кабинете (2.3 доработок): третья дверь входа рядом
с API-ключом и Telegram, тот же путь биллинга. Хелперы продублированы из
других test_*.py намеренно (см. обоснование в test_features_wave2.py)."""

import asyncio
import io

from app import llm
from app.db import SessionLocal
from sqlalchemy import select
from app.models import Customer, WebConversation, WebMessage

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


def _topup(client, admin, amount="50"):
    import re

    client.post("/topups/new", data={"amount_rub": amount})
    m = re.search(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    assert m
    r = admin.post(f"/admin/topups/{m.group(1)}/confirm")
    assert r.status_code in (200, 303)


def _balance(email):
    async def _get():
        async with SessionLocal() as session:
            customer = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one()
            return customer.balance_rub

    return asyncio.run(_get())


def test_chat_page_requires_login(client):
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_send_creates_conversation_and_charges_balance(client, monkeypatch):
    async def _fake_call(alias, messages, **kwargs):
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("у меня всё хорошо")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chat1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    balance_before = _balance("chat1@test.local")

    r = client.post(
        "/chat/send",
        data={"model": "gpt-5-mini", "message": "привет, как дела?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] is not None
    assert body["reply"]

    assert _balance("chat1@test.local") < balance_before

    dashboard_chat = client.get(f"/chat/{body['conversation_id']}")
    assert dashboard_chat.status_code == 200
    assert "привет, как дела?" in dashboard_chat.text


def test_conversation_history_sent_to_provider_on_followup(client, monkeypatch):
    captured = []

    async def _fake_call(alias, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("ответ " + str(len(captured)))

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chat2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    r1 = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "первый вопрос"})
    assert r1.status_code == 200
    conv_id = r1.json()["conversation_id"]

    r2 = client.post(
        "/chat/send", data={"conversation_id": conv_id, "model": "gpt-5-mini", "message": "второй вопрос"}
    )
    assert r2.status_code == 200

    assert len(captured) == 2
    second_call_messages = captured[1]
    joined = " ".join(str(m.get("content")) for m in second_call_messages)
    assert "первый вопрос" in joined
    assert "ответ 1" in joined
    assert "второй вопрос" in joined


class _FakeUsage:
    def __init__(self):
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.prompt_tokens_details = None
        self.cache_creation_input_tokens = None


class _FakeResponse:
    """extract_chat_usage/to_dict читают usage и id как АТРИБУТЫ (getattr),
    а не только из model_dump() — оба пути нужны одновременно."""

    def __init__(self, text):
        self._text = text
        self.id = "chatcmpl-fake"
        self.usage = _FakeUsage()

    def model_dump(self):
        return {
            "id": self.id,
            "choices": [{"message": {"role": "assistant", "content": self._text}}],
            "usage": {"prompt_tokens": self.usage.prompt_tokens, "completion_tokens": self.usage.completion_tokens},
        }


def test_insufficient_balance_returns_402(client):
    _signup(client, "chat3@test.local")
    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "hi", "mock_response": "x"})
    assert r.status_code == 402


def test_unknown_model_returns_404(client):
    _signup(client, "chat4@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    r = client.post("/chat/send", data={"model": "no-such-model", "message": "hi"})
    assert r.status_code == 404


def test_empty_message_without_file_rejected(client):
    _signup(client, "chat5@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "   "})
    assert r.status_code == 400


def test_cannot_view_someone_elses_conversation(client, monkeypatch):
    async def _fake_call(alias, messages, **kwargs):
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("ok")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chatvictim@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "секрет"})
    conv_id = r.json()["conversation_id"]

    from fastapi.testclient import TestClient
    from app.main import app

    attacker = TestClient(app)
    _signup(attacker, "chatattacker@test.local")
    r2 = attacker.get(f"/chat/{conv_id}")
    assert r2.status_code == 404


def test_text_file_attachment_appended_to_message(client, monkeypatch):
    captured = []

    async def _fake_call(alias, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("получил файл")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chatfile1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    r = client.post(
        "/chat/send",
        data={"model": "gpt-5-mini", "message": "что тут написано?"},
        files={"file": ("notes.txt", io.BytesIO("важная заметка".encode("utf-8")), "text/plain")},
    )
    assert r.status_code == 200
    sent_content = str(captured[0][-1]["content"])
    assert "важная заметка" in sent_content
    assert "notes.txt" in sent_content


def test_image_attachment_sent_as_vision_content(client, monkeypatch):
    captured = []

    async def _fake_call(alias, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("вижу картинку")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chatfile2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6360000002000155a2415f0000000049454e44ae426082"
    )
    r = client.post(
        "/chat/send",
        data={"model": "gpt-5-mini", "message": "что на фото?"},
        files={"file": ("photo.png", io.BytesIO(tiny_png), "image/png")},
    )
    assert r.status_code == 200
    last_content = captured[0][-1]["content"]
    assert isinstance(last_content, list)
    types = {p["type"] for p in last_content}
    assert types == {"text", "image_url"}


def test_child_account_blocked_same_as_api(client):
    _signup(client, "chatparent@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    client.post(
        "/children/new",
        data={"email": "chatkid@test.local", "name": "Kid", "password": "KidPass123"},
        follow_redirects=True,
    )

    from fastapi.testclient import TestClient
    from app.main import app

    kid = TestClient(app)
    kid.post("/login", data={"email": "chatkid@test.local", "password": "KidPass123"})
    r = kid.post(
        "/chat/send",
        data={"model": "gpt-5-mini", "message": "напиши сочинение про войну и мир"},
    )
    assert r.status_code == 400
