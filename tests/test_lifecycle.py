"""Worker cleanup must run when the application exits with an exception."""

import asyncio

import pytest
from fastapi import FastAPI

from app.core import lifecycle


def test_lifespan_cancels_workers_after_application_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = [asyncio.Event(), asyncio.Event()]

        async def telegram() -> None:
            started[0].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[0].set()

        async def reaper() -> None:
            started[1].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[1].set()

        async def readiness() -> None:
            return None

        monkeypatch.setattr(lifecycle.llm, "init_router", lambda: None)
        monkeypatch.setattr(lifecycle, "report_model_readiness", readiness)
        monkeypatch.setattr(lifecycle.telegram_bot, "poll_loop", telegram)
        monkeypatch.setattr(lifecycle.reaper, "reaper_loop", reaper)
        monkeypatch.setattr(lifecycle.settings, "telegram_bot_token", "test:dummy")
        with pytest.raises(RuntimeError, match="application error"):
            async with lifecycle.lifespan(FastAPI()):
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
                raise RuntimeError("application error")
        assert all(event.is_set() for event in cancelled)

    asyncio.run(run())
