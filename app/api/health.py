"""api / health for the Flawless application."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.db.models import (
    utcnow,
)
from app.integrations import llm

logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)):
    """Проверка ДОХОДИТ ДО БАЗЫ. Раньше возвращала статичное «ok», не
    заглядывая никуда: внешний монитор рапортовал «сервис жив», пока БД
    лежала и ни один запрос не работал (аудит 2026-09-07)."""
    try:
        await session.execute(select(1))
    except Exception as e:
        logger.error("healthz: database unreachable: %r", e)
        return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)
    # База жива — этого мало. Сервис существует ради вызова моделей, а вызвать
    # можно только модель с действующей ценой: без неё каждый запрос клиента
    # получает 503. Раньше внешний монитор рапортовал «жив», пока сервис не
    # мог обслужить ни одного вызова.
    try:
        models_ready = len(await llm.priced_aliases(session, utcnow()))
    except Exception as e:  # реестр не поднят, конфиг сломан
        logger.error("healthz: model registry unusable: %r", e)
        return JSONResponse({"status": "degraded", "models": "unusable"}, status_code=503)
    if models_ready == 0:
        logger.error("healthz: ни одной модели с действующей ценой — вызовы невозможны")
        return JSONResponse(
            {"status": "degraded", "database": "ok", "models_ready": 0}, status_code=503
        )
    return {"status": "ok", "database": "ok", "models_ready": models_ready}
