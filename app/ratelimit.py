"""Rate limit: скользящее окно в памяти процесса.

Сознательно не Redis — при одном инстансе uvicorn (текущий деплой,
docker-compose с одной репликой) этого достаточно и не требует лишней
инфраструктуры. Если сервис когда-нибудь станет многопроцессным/
многоинстансным — вынести счётчик в Redis, лимит иначе не будет общим.
"""

import time
from collections import defaultdict, deque

from app.config import settings

_hits: dict[str, deque] = defaultdict(deque)
_login_hits: dict[str, deque] = defaultdict(deque)

_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SECONDS = 300


def api_key_bucket(api_key_id: int) -> str:
    return f"key:{api_key_id}"


def catalog_bucket(api_key_id: int) -> str:
    """Отдельный счётчик для /v1/models. Клиенты вроде OpenWebUI дёргают
    справочник при каждом открытии страницы — на общей корзине он съедал бы
    квоту, отведённую на платные вызовы."""
    return f"catalog:{api_key_id}"


def customer_bucket(customer_id: int) -> str:
    return f"customer:{customer_id}"


def check(bucket: str) -> bool:
    """True — запрос разрешён (и уже учтён), False — превышен лимит.

    Ключ — строка с префиксом (см. api_key_bucket/customer_bucket): вызовы
    по API считаются по ключу, а веб-чат и Telegram — по человеку, и голые
    целые id столкнулись бы между собой (ключ №5 и клиент №5 — разные
    сущности, но одно ведро)."""
    now = time.monotonic()
    window = settings.rate_limit_window_seconds
    q = _hits[bucket]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= settings.rate_limit_per_window:
        return False
    q.append(now)
    return True


def check_login(ip: str) -> bool:
    """Отдельный, гораздо более строгий лимит для /login — там нет
    customer_id (это как раз то, что подбирают), ключ по IP. Без этого
    bcrypt-хэш пароля можно перебирать неограниченно."""
    now = time.monotonic()
    q = _login_hits[ip]
    while q and now - q[0] > _LOGIN_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= _LOGIN_LIMIT:
        return False
    q.append(now)
    return True
