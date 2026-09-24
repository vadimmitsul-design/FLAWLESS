"""Process-local sliding-window limits for the single-worker deployment.

State belongs to explicit instances so tests and application composition can
reset or replace it. Multiple processes require a shared backend.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import settings

_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SECONDS = 300


@dataclass
class RateLimiter:
    """Track independently named buckets using a monotonic clock."""

    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)

    def check(self, bucket: str, *, limit: int, window_seconds: float) -> bool:
        now = self.clock()
        hits = self._hits.setdefault(bucket, deque())
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


default_limiter = RateLimiter()
login_limiter = RateLimiter()


def reset() -> None:
    """Reset both default limiters for an application restart or test isolation."""
    default_limiter.reset()
    login_limiter.reset()


def api_key_bucket(api_key_id: int) -> str:
    return f"key:{api_key_id}"


def catalog_bucket(api_key_id: int) -> str:
    """Catalog requests must not consume the quota for paid model calls."""
    return f"catalog:{api_key_id}"


def customer_bucket(customer_id: int) -> str:
    return f"customer:{customer_id}"


def check(bucket: str) -> bool:
    """Check and consume one slot in the configured API/customer bucket."""
    return default_limiter.check(
        bucket,
        limit=settings.rate_limit_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    )


def check_login(ip: str) -> bool:
    """Apply the independent, stricter password-login limit per IP address."""
    return login_limiter.check(ip, limit=_LOGIN_LIMIT, window_seconds=_LOGIN_WINDOW_SECONDS)
