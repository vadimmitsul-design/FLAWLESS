"""Уборщик зависших pending-событий (находка состязательного ревью 2026-09-04,
дополняет finally-фикс в main.py._stream_chat_completion).

Тот finally ловит GeneratorExit/CancelledError — обрыв соединения клиентом
или отмену задачи. Но если процесс убивают целиком (OOM, docker kill -9,
падение хоста) МЕЖДУ start_call и finalize_success/finalize_failure — не
выполнится вообще никакой Python-код, ни except, ни finally. Событие
остаётся 'pending' с reserved_rub навсегда, и это НАВСЕГДА уменьшает
доступный баланс плательщика (см. billing.start_call — активный резерв
считается по всем 'pending' строкам). Единственный способ закрыть этот
случай — периодически подметать снаружи, отдельным процессом/тасков."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app import alerts, billing
from app.config import settings
from app.db import SessionLocal
from app.models import UsageEvent, utcnow

logger = logging.getLogger(__name__)


async def reap_stale_pending_events(session) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.stale_pending_window())
    stale = (
        await session.execute(
            select(UsageEvent).where(UsageEvent.status == "pending", UsageEvent.created_at < cutoff)
        )
    ).scalars().all()
    for event in stale:
        # Денег не списываем — процесс, скорее всего, упал целиком, у нас нет
        # ни подтверждённого usage, ни надёжной частичной оценки (в отличие от
        # обрыва соединения, где контент уже был на руках у генератора).
        # Цель здесь — только освободить резерв, не сборы за неизвестный вызов.
        await billing.finalize_failure(
            session, event, error_code="StaleReservationReaped", latency_ms=0
        )
    return len(stale)


async def reaper_loop() -> None:
    """Периодическое обслуживание: подмести зависшие резервы и проверить,
    не нездоров ли сервис (app/alerts.py). Отдельную фоновую задачу под
    проверки не заводим — цикл уже есть и крутится с нужной частотой."""
    while True:
        try:
            async with SessionLocal() as session:
                count = await reap_stale_pending_events(session)
                if count:
                    logger.warning("reaper: released %d stale pending usage_events (reservation leaks)", count)
                await alerts.check_and_notify(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper: sweep failed")
        await asyncio.sleep(settings.reaper_interval_seconds)
