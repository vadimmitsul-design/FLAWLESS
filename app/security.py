import hashlib
import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ApiKey, Customer


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    return "nh_" + secrets.token_urlsafe(32)


def generate_temp_password() -> str:
    return secrets.token_urlsafe(9)


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
        raise HTTPException(status_code=401, detail={"error": {"message": "missing bearer token", "type": "invalid_request_error"}})
    token = auth.removeprefix("Bearer ").strip()
    stmt = (
        select(Customer, ApiKey)
        .join(ApiKey, ApiKey.customer_id == Customer.id)
        .where(ApiKey.key_hash == hash_api_key(token), ApiKey.active, Customer.active)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid API key", "type": "invalid_request_error"}})
    return row[0], row[1]
