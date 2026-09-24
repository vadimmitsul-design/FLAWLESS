"""services / reporting for the Flawless application."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound, InvalidInput
from app.db.models import (
    Customer,
    UsageEvent,
    utcnow,
)
from app.db.repositories.api_keys import list_customer_keys
from app.integrations import llm
from app.services import billing

USAGE_PAGE_LIMIT = 200


# ---------- веб: кабинет ----------


def _sparkline(values: list[float], width: float = 560.0, height: float = 92.0) -> dict:
    """Путь для SVG-графика расхода по дням.

    Считается на сервере, а не в браузере: график должен быть виден и когда
    скрипты не отработали, и на скриншоте, и в печати.
    """
    if not values:
        return {"line": "", "area": "", "max": 0.0}
    top, bottom = 10.0, height - 12.0
    left, right = 2.0, width - 2.0
    peak = max(values) or 1.0
    points = []
    for i, value in enumerate(values):
        x = left if len(values) == 1 else left + (right - left) * i / (len(values) - 1)
        y = bottom - (bottom - top) * (value / peak)
        points.append((round(x, 1), round(y, 1)))
    line = f"M {points[0][0]} {points[0][1]}"
    for i in range(1, len(points)):
        (px, py), (x, y) = points[i - 1], points[i]
        mid = round((px + x) / 2, 1)
        line += f" C {mid} {py} {mid} {y} {x} {y}"
    area = f"{line} L {points[-1][0]} {height} L {points[0][0]} {height} Z"
    return {"line": line, "area": area, "max": peak, "last": points[-1]}


async def spend_summary(session: AsyncSession, customer: Customer, days: int = 14) -> dict:
    """Сколько человек потратил: итоги, разбивка по дням и место относительно
    его потолков.

    Возвращает ещё и ever_called — были ли вызовы КОГДА-ЛИБО. Итог считается
    за последние 14 дней, а таблица вызовов в кабинете показывает последние
    20 без ограничения по времени: у человека, поработавшего месяц назад,
    первый экран утверждал «Вызовов ещё не было» и тут же показывал вызов со
    списанной суммой.

    Считается по customer_id (кто вызывал), а не по billing_customer_id (с
    чьего кошелька списано): в кабинете человек хочет видеть СВОЙ расход.
    У детского аккаунта платит родитель, но вызовы всё равно его.
    """
    now = utcnow()
    since = now - timedelta(days=days)

    total, calls, tokens = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
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
                UsageEvent.created_at >= since,
                UsageEvent.charged_rub.is_not(None),
            )
        )
    ).one()

    # Группировка по дате средствами БД: тянуть в память все вызовы за две
    # недели нельзя — у активного клиента это десятки тысяч строк.
    # func.date() есть и в SQLite, и в PostgreSQL.
    by_day = dict(
        (str(day), float(amount))
        for day, amount in (
            await session.execute(
                select(
                    func.date(UsageEvent.created_at).label("day"),
                    func.coalesce(func.sum(UsageEvent.charged_rub), 0),
                )
                .where(
                    UsageEvent.customer_id == customer.id,
                    UsageEvent.created_at >= since,
                    UsageEvent.charged_rub.is_not(None),
                )
                .group_by(func.date(UsageEvent.created_at))
            )
        ).all()
    )
    series = [
        by_day.get(str((now - timedelta(days=days - 1 - i)).date()), 0.0) for i in range(days)
    ]

    payer_id = billing.resolve_billing_customer_id(customer)
    spent_today = await billing.spent_since(
        session,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        billing_customer_id=payer_id,
    )
    spent_month = await billing.spent_since(
        session,
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        billing_customer_id=payer_id,
    )

    balance = float(customer.balance_rub)
    per_day = float(total) / days if total else 0.0
    return {
        "days": days,
        "total": float(total),
        "calls": int(calls),
        "tokens": int(tokens),
        "avg_call": float(total) / calls if calls else 0.0,
        "series": series,
        "chart": _sparkline(series),
        "peak_day": max(series) if series else 0.0,
        "spent_today": float(spent_today),
        "spent_month": float(spent_month),
        "daily_limit": float(customer.daily_limit_rub)
        if customer.daily_limit_rub is not None
        else None,
        "monthly_limit": float(customer.monthly_limit_rub)
        if customer.monthly_limit_rub is not None
        else None,
        # Сколько дней протянет баланс при текущем темпе. Оценка грубая, и
        # в интерфейсе это сказано. Больше полугода не показываем: «хватит
        # на 1720 дней» — ложная точность, которая только мешает верить
        # остальным цифрам.
        "days_left": (
            int(balance / per_day)
            if per_day > 0 and balance > 0 and balance / per_day <= 180
            else None
        ),
        "runway_long": bool(per_day > 0 and balance > 0 and balance / per_day > 180),
        "ever_called": bool(
            (
                await session.execute(
                    select(func.count(UsageEvent.id)).where(UsageEvent.customer_id == customer.id)
                )
            ).scalar_one()
        ),
    }


async def usage_history(
    session: AsyncSession,
    customer: Customer,
    key: str,
    month: str | None,
    limit: int | None = None,
) -> dict:
    """Вызовы клиента за месяц, при желании — только по одному ключу.

    Фильтр по ключу возможен потому, что api_key_id пишется в каждое событие.
    Вызовы из веб-чата и Telegram приходят без ключа (api_key_id NULL) — для
    них отдельное значение фильтра, иначе они молча пропадали бы из «всех»
    при любом выборе.
    """
    start, end, label = parse_month(month)
    keys = await list_customer_keys(session, customer.id)

    stmt = select(UsageEvent).where(
        UsageEvent.customer_id == customer.id,
        UsageEvent.created_at >= start,
        UsageEvent.created_at < end,
    )
    if key == "none":
        stmt = stmt.where(UsageEvent.api_key_id.is_(None))
    elif key:
        try:
            key_id = int(key)
        except ValueError as exc:
            raise InvalidInput("ключ указан неверно") from exc
        # Чужой ключ фильтровать нельзя — иначе по номеру можно было бы
        # подсмотреть, сколько вызовов у соседа.
        if key_id not in {k.id for k in keys}:
            raise EntityNotFound("ключ не найден")
        stmt = stmt.where(UsageEvent.api_key_id == key_id)

    stmt = stmt.order_by(UsageEvent.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    events = (await session.execute(stmt)).scalars().all()

    spent = sum((e.charged_rub or Decimal(0)) for e in events)
    return {
        "events": events,
        "keys": keys,
        "key": key,
        "month": label,
        "spent": spent,
        "calls": len(events),
        "alias_of": {(e.provider, e.model): llm.alias_for(e.provider, e.model) for e in events},
    }


# ---------- веб: админ — люди, потребление, приглашения ----------


def parse_month(value: str | None) -> tuple[datetime, datetime, str]:
    """'YYYY-MM' -> границы месяца в UTC. По умолчанию — текущий месяц.
    Границы считаем явными сравнениями created_at, а не приведением к дате
    в SQL: приведение timestamptz к date в Postgres зависит от таймзоны
    сессии, и отчёт молча съезжал бы на границах суток."""
    now = datetime.now(UTC)
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m")
        except ValueError as exc:
            raise InvalidInput("month must be YYYY-MM") from exc
        year, month = parsed.year, parsed.month
    else:
        year, month = now.year, now.month
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
    return start, end, f"{year:04d}-{month:02d}"


def shift_month(label: str, delta: int) -> str:
    year, month = (int(part) for part in label.split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


async def spend_by_customer(session: AsyncSession, start: datetime, end: datetime) -> dict:
    """Расход за период по КОШЕЛЬКАМ (billing_customer_id): для детского
    аккаунта платит родитель, и бюджет расходуется у него."""
    rows = (
        await session.execute(
            select(
                UsageEvent.billing_customer_id,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.charged_rub), 0),
            )
            .where(
                UsageEvent.charged_rub.is_not(None),
                UsageEvent.created_at >= start,
                UsageEvent.created_at < end,
            )
            .group_by(UsageEvent.billing_customer_id)
        )
    ).all()
    return {payer_id: {"calls": calls, "spent": spent} for payer_id, calls, spent in rows}
