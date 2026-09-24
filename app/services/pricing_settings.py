"""Read model for administrative pricing configuration."""

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer
from app.services import billing

EXAMPLE_COST_USD = Decimal("0.01")


async def pricing_settings_context(
    session: AsyncSession, customer: Customer, error: str | None
) -> dict[str, Any]:
    config = await billing.get_pricing_config(session)
    updated_by = (
        await session.get(Customer, config.updated_by_admin_id)
        if config.updated_by_admin_id
        else None
    )
    return {
        "customer": customer,
        "cfg": config,
        "updated_by": updated_by,
        "error": error,
        "example_rub": billing.price_in_rub(EXAMPLE_COST_USD, config),
    }
