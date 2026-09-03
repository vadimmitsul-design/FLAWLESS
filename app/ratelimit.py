"""Rate limit по API-ключу (customer_id): скользящее окно в памяti процесса.

Сознательно не Redis — при одном инстансе uvicorn (текущий деплой,
docker-compose с одной репликой) этого достаточно и не требует лишней
инфраструктуры. Если сервис когда-нибудь станет многопроцессным/
многоинстансным — вынести счётчик в Redis, лимит иначе не будет общим.
"""

import time
from collections import defaultdict, deque

from app.config import settings

_hits: dict[int, deque] = defaultdict(deque)
_login_hits: dict[str, deque] = defaultdict(deque)

_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SECONDS = 300


def check(customer_id: int) -> bool:
    """True — запрос разрешён (и уже учтён), False — превышен лимит."""
    now = time.monotonic()
    window = settings.rate_limit_window_seconds
    q = _hits[customer_id]
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
