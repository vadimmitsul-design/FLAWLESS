"""Queries for customer-owned API keys used by the cabinet and reporting."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey


async def list_customer_keys(
    session: AsyncSession, customer_id: int, *, active_only: bool = False
) -> list[ApiKey]:
    statement = select(ApiKey).where(ApiKey.customer_id == customer_id)
    if active_only:
        statement = statement.where(ApiKey.active)
    statement = statement.order_by(ApiKey.active.desc(), ApiKey.created_at.desc())
    return list((await session.scalars(statement)).all())
