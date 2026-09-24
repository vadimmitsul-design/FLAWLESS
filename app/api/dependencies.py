"""api / dependencies for the Flawless application."""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    settings,
)
from app.core.security import hash_api_key
from app.db import get_session
from app.db.models import ApiKey, Customer


def _require_feature(enabled: bool) -> None:
    """Выключенный раздел отдаёт 404, а не 403: снаружи он должен выглядеть
    так, будто его в этой сборке просто нет. Прятать раздел только из
    навигации мало — адрес продолжал бы работать."""
    if not enabled:
        raise HTTPException(status_code=404)


# Вешаются на сами маршруты через dependencies=[...], а не проверяются внутри
# тела функции: так про них нельзя забыть, дописывая обработчик.
def _feature_public_site() -> None:
    _require_feature(settings.enable_public_site)


def _feature_resources() -> None:
    _require_feature(settings.enable_resources)


def _feature_shop() -> None:
    _require_feature(settings.enable_shop)


def _feature_prompts() -> None:
    _require_feature(settings.enable_prompts)


def _feature_children() -> None:
    _require_feature(settings.enable_children)


def _feature_archive() -> None:
    _require_feature(settings.enable_archive)


def _require_admin(customer: Customer | None) -> RedirectResponse | None:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    if customer.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return None


async def get_current_customer(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Customer | None:
    customer_id = request.session.get("customer_id")
    if customer_id is None:
        return None
    customer = await session.get(Customer, customer_id)
    if customer is None or not customer.active:
        return None
    return customer


async def get_customer_by_api_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> tuple[Customer, ApiKey]:
    """Возвращает (Customer, ApiKey) — конкретный ключ нужен вызывающему коду
    для лимитов расхода НА КЛЮЧ (2.2 доработок), не только для опознания
    клиента."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "missing bearer token", "type": "invalid_request_error"}},
        )
    token = auth.removeprefix("Bearer ").strip()
    stmt = (
        select(Customer, ApiKey)
        .join(ApiKey, ApiKey.customer_id == Customer.id)
        .where(ApiKey.key_hash == hash_api_key(token), ApiKey.active, Customer.active)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "invalid API key", "type": "invalid_request_error"}},
        )
    return row[0], row[1]
