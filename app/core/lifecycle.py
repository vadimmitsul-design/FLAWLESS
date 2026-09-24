"""core / lifecycle for the Flawless application."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import (
    allowed_signup_domains,
    session_secret_is_weak,
    settings,
)
from app.integrations import llm, telegram_bot
from app.services.catalog import report_model_readiness
from app.workers import reaper

logger = logging.getLogger(__name__)
_SIGNUP_MODES = {"invite", "open", "closed"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if session_secret_is_weak(settings.session_secret):
        # Слабый секрет = любая сессия подделывается (кука подписана, но не
        # зашифрована). В проде падаем на старте, в разработке громко ругаемся:
        # молча работать с дырявыми куками нельзя ни в одном режиме.
        # Раньше проверка сравнивала только с "change-me", а в .env.example
        # лежало "change-me-session-secret" — и не срабатывала никогда.
        if settings.environment == "production":
            raise RuntimeError(
                "SESSION_SECRET is default/weak — sessions would be forgeable. "
                'Set a random value of at least 32 characters (python -c "import secrets; '
                'print(secrets.token_urlsafe(48))") before running with ENVIRONMENT=production'
            )
        logger.warning(
            "SESSION_SECRET is default/weak — session cookies are forgeable. "
            "Acceptable locally, MUST be replaced before deploying."
        )
    if settings.signup_mode not in _SIGNUP_MODES:
        raise RuntimeError(
            f"SIGNUP_MODE must be one of {sorted(_SIGNUP_MODES)}, got '{settings.signup_mode}'"
        )
    if settings.signup_mode == "open":
        domains = allowed_signup_domains(settings.signup_allowed_email_domains)
        if domains:
            logger.info(
                "регистрация открыта, но только с почтой: %s",
                ", ".join("@" + d for d in domains),
            )
        else:
            # Не ошибка — так работает клиентский контур. Но во внутреннем
            # это означает, что завести аккаунт может кто угодно из интернета.
            logger.warning(
                "SIGNUP_MODE=open без SIGNUP_ALLOWED_EMAIL_DOMAINS — "
                "зарегистрироваться сможет любой человек с любой почтой"
            )
    llm.init_router()
    await report_model_readiness()
    telegram_task = None
    if settings.telegram_bot_token:
        telegram_task = asyncio.create_task(telegram_bot.poll_loop())
        logger.info("telegram bot polling task started")
    reaper_task = asyncio.create_task(reaper.reaper_loop())
    logger.info("stale-pending-event reaper task started")
    try:
        yield
    finally:
        # Await cancellation so worker sessions close before the event loop.
        reaper_task.cancel()
        if telegram_task is not None:
            telegram_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telegram_task
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
