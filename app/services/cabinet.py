"""Read models for application reporting."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    settings,
)
from app.db.models import (
    Customer,
    TelegramLink,
    TopupRequest,
    UsageEvent,
)
from app.db.repositories.api_keys import list_customer_keys
from app.integrations import llm, telegram_bot
from app.services.catalog import available_models
from app.services.reporting import spend_summary


async def dashboard_context(session: AsyncSession, customer: Customer) -> dict[str, Any]:
    api_keys = await list_customer_keys(session, customer.id, active_only=True)
    events = (
        (
            await session.execute(
                select(UsageEvent)
                .where(UsageEvent.customer_id == customer.id)
                .order_by(UsageEvent.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    my_topups = (
        (
            await session.execute(
                select(TopupRequest)
                .where(TopupRequest.customer_id == customer.id)
                .order_by(TopupRequest.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    children = (
        (
            await session.execute(
                select(Customer)
                .where(Customer.parent_customer_id == customer.id)
                .order_by(Customer.created_at)
            )
        )
        .scalars()
        .all()
        if not customer.is_child
        else []
    )
    telegram_link = (
        await session.execute(select(TelegramLink).where(TelegramLink.customer_id == customer.id))
    ).scalar_one_or_none()
    return {
        "api_keys": api_keys,
        "events": events,
        "topups": my_topups,
        "models": await available_models(session),
        "children": children,
        "telegram_enabled": bool(settings.telegram_bot_token),
        "telegram_linked": telegram_link is not None,
        "telegram_bot_username": telegram_bot.bot_username,
        "spend": await spend_summary(session, customer),
        "alias_of": {(e.provider, e.model): llm.alias_for(e.provider, e.model) for e in events},
    }
