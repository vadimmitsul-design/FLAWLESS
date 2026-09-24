"""Issue, revoke and configure API keys without exposing stored credentials."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound
from app.core.security import generate_api_key, hash_api_key
from app.db.models import ApiKey, Customer


async def issue_key(session: AsyncSession, customer_id: int, name: str) -> str:
    raw_key = generate_api_key()
    session.add(
        ApiKey(
            customer_id=customer_id,
            name=name.strip() or "Без названия",
            key_hash=hash_api_key(raw_key),
            last_four=raw_key[-4:],
        )
    )
    await session.flush()
    return raw_key


async def _owned_key(session: AsyncSession, key_id: int, customer_id: int) -> ApiKey:
    key = await session.get(ApiKey, key_id)
    if key is None or key.customer_id != customer_id:
        raise EntityNotFound("Not Found")
    return key


async def revoke_key(session: AsyncSession, key_id: int, customer_id: int) -> None:
    key = await _owned_key(session, key_id, customer_id)
    key.active = False
    await session.flush()


async def set_customer_limits(
    session: AsyncSession,
    key_id: int,
    customer_id: int,
    *,
    daily: Decimal | None,
    monthly: Decimal | None,
) -> None:
    key = await _owned_key(session, key_id, customer_id)
    key.daily_limit_rub = daily
    key.monthly_limit_rub = monthly
    await session.flush()


async def set_admin_limits(
    session: AsyncSession, key_id: int, *, daily: Decimal | None, monthly: Decimal | None
) -> None:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise EntityNotFound("Not Found")
    key.admin_daily_limit_rub = daily
    key.admin_monthly_limit_rub = monthly
    await session.flush()


async def active_keys_with_owners(session: AsyncSession) -> list[tuple[ApiKey, Customer]]:
    rows = await session.execute(
        select(ApiKey, Customer)
        .join(Customer, Customer.id == ApiKey.customer_id)
        .where(ApiKey.active)
        .order_by(ApiKey.created_at.desc())
    )
    return [(key, customer) for key, customer in rows]
