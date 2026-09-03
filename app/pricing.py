"""Расчёт себестоимости по model_prices на момент вызова.

LiteLLM-овский response_cost пишется рядом как контрольное значение,
но источником истины является наша таблица цен. Идентично AI-HUB/gateway —
намеренно не меняем, обе таблицы прайсов ведутся тем же способом.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelPrice

_MILLION = Decimal(1_000_000)
_CENT_MICRO = Decimal("0.000001")


@dataclass
class UsageAmounts:
    images_count: int | None = None
    input_text_tokens: int | None = None
    input_image_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None


def select_best_price(
    rows: Sequence[ModelPrice], quality: str | None, size: str | None
) -> ModelPrice | None:
    """Из действующих строк прайса выбирает самую специфичную подходящую.

    NULL в quality/size строки прайса означает "любое значение". При равной
    специфичности приоритет у совпадения по quality.
    """
    candidates = [
        r
        for r in rows
        if (r.quality is None or r.quality == quality)
        and (r.size is None or r.size == size)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.quality is not None, r.size is not None))


async def find_price(
    session: AsyncSession,
    provider: str,
    model: str,
    quality: str | None,
    size: str | None,
    at: datetime,
) -> ModelPrice | None:
    stmt = select(ModelPrice).where(
        ModelPrice.provider == provider,
        ModelPrice.model == model,
        ModelPrice.valid_from <= at,
        or_(ModelPrice.valid_until.is_(None), ModelPrice.valid_until > at),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return select_best_price(rows, quality, size)


def compute_cost(price: ModelPrice | None, usage: UsageAmounts) -> Decimal | None:
    """Суммирует все компоненты, для которых есть и тариф, и факт расхода.
    None — посчитать нечем (нет строки прайса или провайдер не вернул usage);
    такие события ловятся мониторингом по cost IS NULL.
    """
    if price is None:
        return None

    total = Decimal(0)
    priced = False

    def add(rate: Decimal | None, tokens: int | None) -> None:
        nonlocal total, priced
        if rate is not None and tokens:
            total += rate * Decimal(tokens) / _MILLION
            priced = True

    if price.price_per_image is not None and usage.images_count:
        total += price.price_per_image * usage.images_count
        priced = True
    add(price.price_per_1m_input_tokens, usage.input_text_tokens)
    add(price.price_per_1m_input_image_tokens, usage.input_image_tokens)
    add(price.price_per_1m_output_tokens, usage.output_tokens)
    add(price.price_per_1m_cached_tokens, usage.cached_tokens)

    if not priced:
        return None
    return total.quantize(_CENT_MICRO)
