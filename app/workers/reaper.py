"""Release reservations left behind by a terminated application process.

Stream cleanup cannot run after OOM or a host failure. The periodic sweep
closes old pending events through the same guarded billing finalizer used by
request processing, without charging unconfirmed usage.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import SessionLocal
from app.db.models import UsageEvent, utcnow
from app.services import billing
from app.workers import alerts

logger = logging.getLogger(__name__)


async def reap_stale_pending_events(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.stale_pending_window())
    stale = (
        (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.status == "pending", UsageEvent.created_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    reaped = 0
    for event in stale:
        # Денег не списываем — процесс, скорее всего, упал целиком, у нас нет
        # ни подтверждённого usage, ни надёжной частичной оценки (в отличие от
        # обрыва соединения, где контент уже был на руках у генератора).
        # Цель здесь — только освободить резерв, не сборы за неизвестный вызов.
        if await billing.reap_pending_call(session, event):
            reaped += 1
    return reaped


async def reaper_loop() -> None:
    """Периодическое обслуживание: подмести зависшие резервы и проверить,
    не нездоров ли сервис (app/workers/alerts.py). Отдельную фоновую задачу под
    проверки не заводим — цикл уже есть и крутится с нужной частотой."""
    while True:
        try:
            async with SessionLocal() as session:
                count = await reap_stale_pending_events(session)
                if count:
                    logger.warning(
                        "reaper: released %d stale pending usage_events (reservation leaks)", count
                    )
                await alerts.check_and_notify(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper: sweep failed")
        await asyncio.sleep(settings.reaper_interval_seconds)
