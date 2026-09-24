"""Read models for application reporting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    settings,
)
from app.core.errors import InvalidInput
from app.db.models import (
    Customer,
    UsageEvent,
    WalletLedger,
)


async def overview_context(session: AsyncSession) -> dict[str, Any]:
    total_topped_up = (
        await session.execute(
            select(func.coalesce(func.sum(WalletLedger.delta_rub), 0)).where(
                WalletLedger.entry_type == "topup"
            )
        )
    ).scalar_one()
    # Обязательство перед клиентами — сколько ещё могут потратить.
    outstanding_balance = (
        await session.execute(
            select(func.coalesce(func.sum(Customer.balance_rub), 0)).where(
                Customer.role == "customer"
            )
        )
    ).scalar_one()
    # Признанная выручка — списано за реальное использование токенов.
    recognized_revenue = (
        await session.execute(
            select(func.coalesce(func.sum(-WalletLedger.delta_rub), 0)).where(
                WalletLedger.entry_type == "usage"
            )
        )
    ).scalar_one()
    # Себестоимость у провайдеров, переведённая в рубли по курсу на момент вызова.
    # По charged_rub IS NOT NULL, не по status=='success' — событие с честным
    # списанием по оценке при обрыве стрима (1.3 доработок, billing_estimated)
    # имеет status='failed', но реальная себестоимость и списание у него есть.
    cost_rub = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(
                UsageEvent.charged_rub.is_not(None)
            )
        )
    ).scalar_one() or Decimal(0)
    customer_count = (
        await session.execute(select(func.count(Customer.id)).where(Customer.role == "customer"))
    ).scalar_one()
    failed_events = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(UsageEvent.status == "failed")
        )
    ).scalar_one()
    uncosted_events = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.status == "success", UsageEvent.cost_usd.is_(None)
            )
        )
    ).scalar_one()

    # 1.6 доработок: маржа по каждой модели, не только общая — изменение
    # цены у поставщика (или ошибка в model_prices) должно быть видно
    # по конкретной модели, а не тонуть в среднем по больнице.
    by_model_rows = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
            )
            .where(UsageEvent.charged_rub.is_not(None))
            .group_by(UsageEvent.model)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()
    by_model = []
    for model, count, revenue, cost in by_model_rows:
        margin = revenue - cost
        margin_pct = (margin / revenue * 100) if revenue else None
        by_model.append(
            {
                "model": model,
                "count": count,
                "revenue": revenue,
                "cost": cost,
                "margin": margin,
                "margin_pct": margin_pct,
                "low_margin": margin_pct is not None
                and margin_pct < settings.margin_alert_threshold_pct,
            }
        )

    recent_customers = (
        (
            await session.execute(
                select(Customer)
                .where(Customer.role == "customer")
                .order_by(Customer.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    return {
        "settings": settings,
        "total_topped_up": total_topped_up,
        "outstanding_balance": outstanding_balance,
        "recognized_revenue": recognized_revenue,
        "cost_rub": cost_rub,
        "margin_rub": recognized_revenue - cost_rub,
        "customer_count": customer_count,
        "failed_events": failed_events,
        "uncosted_events": uncosted_events,
        "by_model": by_model,
        "recent_customers": recent_customers,
    }


async def reconciliation_context(session: AsyncSession, d: str | None) -> dict[str, Any]:
    if d:
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError as exc:
            raise InvalidInput("date must be YYYY-MM-DD") from exc
    else:
        day = (datetime.now(UTC) - timedelta(days=1)).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    charged_today = (
        UsageEvent.charged_rub.is_not(None)
        & (UsageEvent.created_at >= day_start)
        & (UsageEvent.created_at < day_end)
    )

    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.charged_rub), 0)).where(charged_today)
        )
    ).scalar_one() or Decimal(0)
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(
                charged_today
            )
        )
    ).scalar_one() or Decimal(0)
    total_count = (
        await session.execute(select(func.count(UsageEvent.id)).where(charged_today))
    ).scalar_one()

    litellm_covered = charged_today & UsageEvent.litellm_cost.is_not(None)
    litellm_cost_rub = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.litellm_cost * UsageEvent.usd_rub_rate), 0)
            ).where(litellm_covered)
        )
    ).scalar_one() or Decimal(0)
    cost_for_covered = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0)).where(
                litellm_covered
            )
        )
    ).scalar_one() or Decimal(0)
    litellm_covered_count = (
        await session.execute(select(func.count(UsageEvent.id)).where(litellm_covered))
    ).scalar_one()

    discrepancy_rub = cost_for_covered - litellm_cost_rub
    discrepancy_pct = (discrepancy_rub / litellm_cost_rub * 100) if litellm_cost_rub else None
    high_discrepancy = (
        discrepancy_pct is not None
        and abs(discrepancy_pct) > settings.cost_discrepancy_alert_threshold_pct
    )

    margin = revenue - cost
    margin_pct = (margin / revenue * 100) if revenue else None
    low_margin = margin_pct is not None and margin_pct < settings.margin_alert_threshold_pct

    by_model_rows = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd * UsageEvent.usd_rub_rate), 0),
            )
            .where(charged_today)
            .group_by(UsageEvent.model)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()
    by_model = []
    for model, count, model_revenue, model_cost in by_model_rows:
        model_margin = model_revenue - model_cost
        model_margin_pct = (model_margin / model_revenue * 100) if model_revenue else None
        by_model.append(
            {
                "model": model,
                "count": count,
                "revenue": model_revenue,
                "cost": model_cost,
                "margin": model_margin,
                "margin_pct": model_margin_pct,
                "low_margin": model_margin_pct is not None
                and model_margin_pct < settings.margin_alert_threshold_pct,
            }
        )
    return {
        "settings": settings,
        "day": day,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "is_today": day >= datetime.now(UTC).date(),
        "revenue": revenue,
        "cost": cost,
        "margin": margin,
        "margin_pct": margin_pct,
        "low_margin": low_margin,
        "total_count": total_count,
        "litellm_cost_rub": litellm_cost_rub,
        "cost_for_covered": cost_for_covered,
        "discrepancy_rub": discrepancy_rub,
        "discrepancy_pct": discrepancy_pct,
        "high_discrepancy": high_discrepancy,
        "litellm_covered_count": litellm_covered_count,
        "by_model": by_model,
    }
