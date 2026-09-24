"""api / v1 / models for the Flawless application."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_customer_by_api_key
from app.core import ratelimit
from app.db import get_session
from app.db.models import (
    ApiKey,
    Customer,
)
from app.services.catalog import catalog_objects

router = APIRouter()


@router.get("/v1/models")
async def list_models(
    auth: tuple[Customer, ApiKey] = Depends(get_customer_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    """Справочник моделей в формате OpenAI.

    Нужен не для красоты: Cursor, OpenWebUI, LibreChat и прочие готовые
    клиенты спрашивают список первым делом и без него либо не подключаются,
    либо требуют вводить имя модели руками.

    Отдаются только модели с действующей ценой — ровно те, что вызов
    реально примет.
    """
    _customer, api_key = auth
    if not ratelimit.check(ratelimit.catalog_bucket(api_key.id)):
        raise HTTPException(
            status_code=429,
            detail={
                "error": {"message": "rate limit exceeded, slow down", "type": "rate_limit_error"}
            },
        )
    return {"object": "list", "data": await catalog_objects(session)}


@router.get("/v1/models/{model_id}")
async def retrieve_model(
    model_id: str,
    auth: tuple[Customer, ApiKey] = Depends(get_customer_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    _customer, api_key = auth
    if not ratelimit.check(ratelimit.catalog_bucket(api_key.id)):
        raise HTTPException(
            status_code=429,
            detail={
                "error": {"message": "rate limit exceeded, slow down", "type": "rate_limit_error"}
            },
        )
    for obj in await catalog_objects(session):
        if obj["id"] == model_id:
            return obj
    raise HTTPException(
        status_code=404,
        detail={
            "error": {"message": f"unknown model '{model_id}'", "type": "invalid_request_error"}
        },
    )
