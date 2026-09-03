"""Origin-проверка вместо CSRF-токена в каждой форме. У сервиса ~15 форм на
сессионной куке (SameSite=Lax уже блокирует классический межсайтовый POST в
современных браузерах — см. main.py, — но это неявная защита, а не
осознанная). Токен в каждый шаблон и роут тянуть дорого и легко забыть в
новой форме — эта проверка одна на всё приложение и покрывает будущие формы
автоматически.

Пропускаем, если Origin/Referer вообще отсутствуют (curl, серверные клиенты,
тестовый TestClient — не браузер жертвы, не CSRF-сценарий) — блокируем только
явное несовпадение домена, это и есть настоящая атака."""

from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_EXEMPT_PREFIXES = ("/v1/",)  # API — Bearer-ключ, не кука; легитимные не-браузерные вызовы


class CSRFOriginMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if request.method in _SAFE_METHODS or request.url.path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin is not None and urlparse(origin).netloc != request.url.netloc:
            response = PlainTextResponse("cross-site request blocked", status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
