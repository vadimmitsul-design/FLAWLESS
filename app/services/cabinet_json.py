"""Bounded, credential-free read models for the browser cabinet."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import EntityNotFound
from app.db.models import Customer, TopupRequest, UsageEvent, WebConversation, as_utc, utcnow
from app.db.repositories.api_keys import list_customer_keys
from app.integrations import llm
from app.services import billing, conversations
from app.services.catalog import available_models
from app.services.reporting import parse_month


def customer_summary(customer: Customer) -> dict[str, Any]:
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "is_child": customer.is_child,
    }


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _date(value: datetime) -> str:
    aware = as_utc(value)
    assert aware is not None
    return aware.isoformat()


async def dashboard(session: AsyncSession, customer: Customer, config: Settings) -> dict[str, Any]:
    """Keep actor history private while reporting the wallet that pays for calls."""
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start, month_end, _ = parse_month(None)
    payer_id = billing.resolve_billing_customer_id(customer)
    payer = customer if payer_id == customer.id else await session.get(Customer, payer_id)
    if payer is None:
        raise EntityNotFound("Кошелёк не найден")
    available = await billing.available_balance(session, payer_id, payer.balance_rub)
    spent_today = await billing.spent_since(session, today, billing_customer_id=payer_id)
    spent_month = await billing.spent_since(session, month_start, billing_customer_id=payer_id)
    requests, tokens = (
        await session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(
                    func.sum(
                        func.coalesce(UsageEvent.input_tokens, 0)
                        + func.coalesce(UsageEvent.output_tokens, 0)
                    ),
                    0,
                ),
            ).where(
                UsageEvent.customer_id == customer.id,
                UsageEvent.created_at >= month_start,
                UsageEvent.created_at < month_end,
            )
        )
    ).one()
    rows = (
        await session.execute(
            select(
                func.date(UsageEvent.created_at),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                func.count(UsageEvent.id),
                func.coalesce(
                    func.sum(
                        func.coalesce(UsageEvent.input_tokens, 0)
                        + func.coalesce(UsageEvent.output_tokens, 0)
                    ),
                    0,
                ),
            )
            .where(
                UsageEvent.customer_id == customer.id,
                UsageEvent.created_at >= today - timedelta(days=6),
                UsageEvent.created_at < today + timedelta(days=1),
            )
            .group_by(func.date(UsageEvent.created_at))
        )
    ).all()
    daily = {str(day): (charged, calls, token_count) for day, charged, calls, token_count in rows}
    daily_usage = []
    for offset in range(6, -1, -1):
        day = str((today - timedelta(days=offset)).date())
        charged, calls, token_count = daily.get(day, (Decimal(0), 0, 0))
        daily_usage.append(
            {"date": day, "charged_rub": _money(charged), "requests": calls, "tokens": token_count}
        )
    events = (
        await session.scalars(
            select(UsageEvent)
            .where(UsageEvent.customer_id == customer.id)
            .order_by(UsageEvent.created_at.desc())
            .limit(50)
        )
    ).all()
    topups = (
        await session.scalars(
            select(TopupRequest)
            .where(TopupRequest.customer_id == customer.id)
            .order_by(TopupRequest.created_at.desc())
            .limit(20)
        )
    ).all()
    history = (
        await session.scalars(
            select(WebConversation)
            .where(WebConversation.customer_id == customer.id)
            .order_by(WebConversation.updated_at.desc())
            .limit(50)
        )
    ).all()
    keys = await list_customer_keys(session, customer.id, active_only=True)
    return {
        "customer": customer_summary(customer),
        "wallet": {
            "balance_rub": _money(payer.balance_rub),
            "reserved_rub": _money(payer.balance_rub - available),
            "available_rub": _money(available),
            "spent_today_rub": _money(spent_today),
            "spent_month_rub": _money(spent_month),
            "daily_limit_rub": _money(payer.daily_limit_rub),
            "monthly_limit_rub": _money(payer.monthly_limit_rub),
        },
        "stats": {"requests_month": requests, "tokens_month": tokens},
        "models": await available_models(session),
        "usage": [
            {
                "id": str(event.id),
                "created_at": _date(event.created_at),
                "model": llm.alias_for(event.provider, event.model) or event.model,
                "input_tokens": event.input_tokens or 0,
                "output_tokens": event.output_tokens or 0,
                "charged_rub": _money(event.charged_rub or Decimal(0)),
                "status": event.status,
            }
            for event in events
        ],
        "daily_usage": daily_usage,
        "api_keys": [
            {
                "id": key.id,
                "name": key.name,
                "prefix": "nh_••••" + key.last_four,
                "created_at": _date(key.created_at),
                "daily_limit_rub": _money(key.daily_limit_rub),
                "monthly_limit_rub": _money(key.monthly_limit_rub),
            }
            for key in keys
        ],
        "topups": [
            {
                "id": topup.id,
                "amount_rub": _money(topup.amount_rub),
                "status": topup.status,
                "note": topup.note,
                "created_at": _date(topup.created_at),
            }
            for topup in topups
        ],
        "conversations": [
            {
                "id": item.id,
                "title": item.title,
                "model": item.model_alias,
                "updated_at": _date(item.updated_at),
            }
            for item in history
        ],
        "flags": {
            "resources": config.enable_resources,
            "shop": config.enable_shop,
            "prompts": config.enable_prompts,
            "children": config.enable_children and not customer.is_child,
            "archive": config.enable_archive,
            "telegram": bool(config.telegram_bot_token),
        },
    }


async def conversation(
    session: AsyncSession, customer_id: int, conversation_id: int
) -> dict[str, Any]:
    page = await conversations.conversation_page(session, customer_id, conversation_id)
    item = page.active_conversation
    assert item is not None
    return {
        "id": item.id,
        "title": item.title,
        "model": item.model_alias,
        "messages": page.messages,
    }
