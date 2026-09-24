"""core / middleware for the Flawless application."""

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import (
    settings,
)


# docs_url/redoc_url/openapi_url=None по двум причинам. Первая: адрес /docs
# занят нашей собственной документацией для клиентов. Вторая: встроенная
# схема FastAPI отдавалась анонимно и перечисляла ВСЕ маршруты, включая
# выключенные флагами разделы (находка разбора 2026-09-08).
class BodySizeLimitMiddleware:
    """Потолок на размер запроса ДО разбора тела.

    Проверка «файл не больше 5 МБ» в обработчике чата срабатывала уже после
    того, как файл целиком прочитан: FastAPI разбирает multipart раньше, чем
    решает зависимости, то есть раньше проверки сессии. Любой человек из
    интернета, без аккаунта, мог одним POST заставить сервис принять и
    сбуферизовать файл произвольного размера.

    Смотрим Content-Length: он есть у любого обычного загрузчика. Запрос без
    него (chunked) этой проверкой не ловится — там режет уже обработчик,
    читающий чанками.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > settings.max_request_body_bytes:
                        response = JSONResponse(
                            {"detail": "тело запроса слишком велико"}, status_code=413
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        await self.app(scope, receive, send)


class NoStoreMiddleware:
    """Запрещает браузеру и промежуточным прокси кэшировать ЛЮБОЙ ответ.

    Ни одна страница в сервисе не годится для кэша: разметка каждой зависит
    от того, вошёл ли человек (шапка кабинета против анонимной, «В кабинет»
    против «Создать аккаунт»), а сервис не отдаёт ни одного статического
    файла отдельно от HTML — кэшировать в принципе нечего, кроме как во
    вред. Без заголовка нашёлся живой случай (2026-09-21): администратор
    открывает /docs анонимно ДО входа, затем логинится и переходит в
    документацию по ссылке из кабинета — браузер вместо нового запроса к
    серверу подставляет старый ответ из своего HTTP-кэша, показывая
    анонимную шапку с «Создать аккаунт» поверх уже активной сессии. Сервер
    при этом всё отдаёт верно (проверено тем же куки через curl) — дыра
    была именно в отсутствии заголовка, разрешающего браузеру решать
    самому.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"cache-control", b"no-store, private"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send_with_no_store)
