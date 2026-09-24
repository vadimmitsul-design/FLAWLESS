"""Чат в кабинете: markdown-рендер ответа на клиенте вместо сырых звёздочек,
SVG-иконка скрепки вместо эмодзи (2026-09-21).

Сам рендер markdown — JS, тестами на Python его логику не потрогать; здесь
проверяется то, что действительно на стороне сервера: ответ модели уходит
в разметку ЭКРАНИРОВАННЫМ (в атрибуте data-raw), а не как есть — иначе
ответ с HTML/скриптом внутри выполнился бы в браузере того, кто его читает.
"""

from app.integrations import llm

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
    assert admin.post(f"/admin/topups/{m.group(1)}/confirm").status_code in (200, 303)


class _FakeUsage:
    def __init__(self):
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.prompt_tokens_details = None
        self.cache_creation_input_tokens = None


class _FakeResponse:
    def __init__(self, text):
        self._text = text
        self.id = "chatcmpl-fake"
        self.usage = _FakeUsage()

    def model_dump(self):
        return {
            "id": self.id,
            "choices": [{"message": {"role": "assistant", "content": self._text}}],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
        }


def test_chat_page_has_svg_paperclip_not_emoji(client):
    """Была эмодзи 📎 — на разных ОС/шрифтах рисуется со сдвинутым центром
    ('кривая', по формулировке заказчика). Теперь это SVG — центрируется
    предсказуемо в любом окружении."""
    _signup(client, "chatui1@test.local")
    page = client.get("/chat").text
    assert "📎" not in page
    assert 'id="chat-file"' in page
    assert "<svg" in page


def test_assistant_reply_with_html_is_escaped_in_the_page_not_executable(client, monkeypatch):
    """Ответ модели может содержать что угодно, включая чужой HTML/скрипт —
    он уходит в data-raw ЭКРАНИРОВАННЫМ, браузер рисует его текстом через
    рендерер, а не парсит как разметку страницы."""
    dangerous_reply = '<img src=x onerror="alert(1)"> и **жирный** текст'

    async def _fake_call(alias, messages, **kwargs):
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse(dangerous_reply)

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chatui2@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "покажи опасный html"})
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]
    assert (
        r.json()["reply"] == dangerous_reply
    )  # JSON-ответ — сырой текст как есть, это не HTML-контекст

    page = client.get(f"/chat/{conv_id}").text
    # Опасная разметка не должна встретиться в исполняемом виде.
    assert "<img src=x onerror" not in page
    # Она должна быть внутри экранированного атрибута data-raw.
    assert (
        "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in page
        or "&lt;img src=x onerror=&#34;alert(1)&#34;&gt" in page
    )
    assert 'class="msg-assistant md-render" data-raw="' in page


def test_conversation_page_has_no_leftover_thinking_placeholder_text(client, monkeypatch):
    """«Думаю…» раньше жило текстом кнопки; теперь это отдельный визуальный
    индикатор, который заменяется реальным ответом и не должен утекать
    в сохранённую историю диалога."""

    async def _fake_call(alias, messages, **kwargs):
        return alias, "openrouter", "openai/gpt-5-mini", _FakeResponse("готово")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_call)

    _signup(client, "chatui3@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    r = client.post("/chat/send", data={"model": "gpt-5-mini", "message": "привет"})
    conv_id = r.json()["conversation_id"]
    page = client.get(f"/chat/{conv_id}").text
    assert "Думаю…" not in page
