"""Read models for application reporting."""

from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound, InvalidInput
from app.db.models import (
    Customer,
    UsageEvent,
    WalletLedger,
)
from app.services.reporting import parse_month, shift_month, spend_by_customer


async def get_customer(session: AsyncSession, customer_id: int) -> Customer:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise EntityNotFound("Not Found")
    return customer


async def toggle_active(session: AsyncSession, customer_id: int, admin_id: int) -> None:
    customer = await get_customer(session, customer_id)
    if customer.id == admin_id:
        raise InvalidInput("cannot disable your own account")
    customer.active = not customer.active
    await session.flush()


async def update_card(
    session: AsyncSession, customer_id: int, *, name: str, job_title: str, department: str
) -> None:
    customer = await get_customer(session, customer_id)
    if not name.strip():
        raise InvalidInput("имя не может быть пустым")
    customer.name = name.strip()
    customer.job_title = job_title.strip() or None
    customer.department = department.strip() or None
    await session.flush()


async def set_limits(
    session: AsyncSession, customer_id: int, *, daily: Decimal | None, monthly: Decimal | None
) -> None:
    customer = await get_customer(session, customer_id)
    for amount in (daily, monthly):
        if amount is not None and (not amount.is_finite() or amount < 0):
            raise InvalidInput("limit must not be negative")
    customer.daily_limit_rub = daily
    customer.monthly_limit_rub = monthly
    await session.flush()


async def customer_export(session: AsyncSession, month: str | None) -> dict[str, Any]:
    start, end, label = parse_month(month)
    spend = await spend_by_customer(session, start, end)
    customers = list((await session.scalars(select(Customer).order_by(Customer.name))).all())
    return {"customers": customers, "spend": spend, "month": label}


async def customer_detail(
    session: AsyncSession, customer_id: int, month: str | None
) -> dict[str, Any]:
    target = await session.get(Customer, customer_id)
    if target is None:
        raise EntityNotFound("Not Found")
    start, end, label = parse_month(month)

    in_period = (
        UsageEvent.billing_customer_id == target.id,
        UsageEvent.charged_rub.is_not(None),
        UsageEvent.created_at >= start,
        UsageEvent.created_at < end,
    )

    by_model = [
        {"model": model, "calls": calls, "spent": spent, "cost": cost}
        for model, calls, spent, cost in (
            await session.execute(
                select(
                    UsageEvent.model,
                    func.count(UsageEvent.id),
                    func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                    func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
                )
                .where(*in_period)
                .group_by(UsageEvent.model)
                .order_by(func.coalesce(func.sum(UsageEvent.charged_rub), 0).desc())
            )
        ).all()
    ]

    # По дням группируем в Python: приведение timestamptz к дате в SQL зависит
    # от таймзоны сессии Postgres, и сутки могли бы съезжать. Данных здесь —
    # события одного человека за один месяц, это дёшево.
    daily: dict[str, dict] = {}
    for created_at, charged in (
        await session.execute(
            select(UsageEvent.created_at, UsageEvent.charged_rub).where(*in_period)
        )
    ).all():
        day = created_at.astimezone(UTC).strftime("%d.%m")
        bucket = daily.setdefault(day, {"calls": 0, "spent": Decimal(0)})
        bucket["calls"] += 1
        bucket["spent"] += charged
    by_day = [{"day": day, **stats} for day, stats in sorted(daily.items(), reverse=True)]

    period_spent = sum((row["spent"] for row in by_model), Decimal(0))
    period_calls = sum(row["calls"] for row in by_model)
    lifetime_spent = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.charged_rub), 0)).where(
                UsageEvent.billing_customer_id == target.id, UsageEvent.charged_rub.is_not(None)
            )
        )
    ).scalar_one()

    ledger = (
        (
            await session.execute(
                select(WalletLedger)
                .where(WalletLedger.customer_id == target.id)
                .order_by(WalletLedger.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return {
        "target": target,
        "ledger": ledger,
        "month": label,
        "prev_month": shift_month(label, -1),
        "next_month": shift_month(label, 1),
        "is_current_month": label == parse_month(None)[2],
        "by_model": by_model,
        "by_day": by_day,
        "period_spent": period_spent,
        "period_calls": period_calls,
        "lifetime_spent": lifetime_spent,
    }


async def customer_report(session: AsyncSession, month: str | None) -> dict[str, Any]:
    start, end, label = parse_month(month)
    spend = await spend_by_customer(session, start, end)
    customers = (
        (
            await session.execute(
                select(Customer).order_by(Customer.active.desc(), Customer.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    rows = [
        {
            "c": c,
            "calls": spend.get(c.id, {}).get("calls", 0),
            "spent": spend.get(c.id, {}).get("spent", Decimal(0)),
        }
        for c in customers
    ]
    rows.sort(key=lambda r: (r["spent"], r["calls"]), reverse=True)
    return {
        "rows": rows,
        "month": label,
        "prev_month": shift_month(label, -1),
        "next_month": shift_month(label, 1),
        "is_current_month": label == parse_month(None)[2],
        "total_spent": sum((r["spent"] for r in rows), Decimal(0)),
        "total_calls": sum(r["calls"] for r in rows),
    }
