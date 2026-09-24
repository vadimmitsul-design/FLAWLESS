"""Deterministic checks of independent sliding-window limiter instances."""

from app.core.ratelimit import RateLimiter


def test_window_keeps_boundary_and_rejects_without_consuming_a_slot():
    now = 0.0
    limiter = RateLimiter(clock=lambda: now)
    assert limiter.check("key:1", limit=1, window_seconds=60)
    now = 60.0
    assert not limiter.check("key:1", limit=1, window_seconds=60)
    now = 60.001
    assert limiter.check("key:1", limit=1, window_seconds=60)


def test_limiter_instances_and_bucket_names_are_independent():
    first = RateLimiter(clock=lambda: 0.0)
    second = RateLimiter(clock=lambda: 0.0)
    assert first.check("key:1", limit=1, window_seconds=60)
    assert not first.check("key:1", limit=1, window_seconds=60)
    assert first.check("customer:1", limit=1, window_seconds=60)
    assert second.check("key:1", limit=1, window_seconds=60)
    first.reset()
    assert first.check("key:1", limit=1, window_seconds=60)
