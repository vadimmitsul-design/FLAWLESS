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

from app.config import settings
from app.models import ModelPrice

_MILLION = Decimal(1_000_000)
_CENT_MICRO = Decimal("0.000001")


@dataclass
class UsageAmounts:
    images_count: int | None = None
    input_text_tokens: int | None = None
    input_image_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
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
    add(price.price_per_1m_cache_write_tokens, usage.cache_write_tokens)

    if not priced:
        return None
    return total.quantize(_CENT_MICRO)


_CHARS_PER_TOKEN = 4


def _estimate_tokens_from_char_count(char_count: int) -> int:
    return max(1, char_count // _CHARS_PER_TOKEN) if char_count else 0


def estimate_tokens_from_text(text: str) -> int:
    """Грубая оценка ~4 символа/токен — используется ТОЛЬКО там, где точного
    числа ещё нет (резерв под ещё не отправленный вызов, см. 1.1 доработок)
    или уже не будет (обрыв стрима до финального usage-чанка, см. 1.3).
    Никогда не подменяет реальный usage от провайдера, когда он есть."""
    return _estimate_tokens_from_char_count(len(text) if text else 0)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
    return _estimate_tokens_from_char_count(total_chars)


def clamp_output_tokens(extra: dict) -> dict:
    """Всегда отправляем провайдеру явный предел длины ответа, ограниченный
    settings.max_output_tokens_cap.

    Без этого резерв считался исходя из 4096 токенов ответа, но никто не
    заставлял провайдера в них уложиться: ответ на 64к токенов списывался
    целиком уже ПОСЛЕ факта, без проверки баланса, и уводил его в минус.
    Клиентское значение не игнорируем, а зажимаем сверху — и тем же числом
    считается резерв (estimate_output_tokens_hint читает этот же extra),
    так что оценка и реальность сходятся по построению."""
    cap = settings.max_output_tokens_cap
    clamped = dict(extra)
    for field in ("max_completion_tokens", "max_tokens"):
        value = clamped.get(field)
        if isinstance(value, int) and value > 0:
            clamped[field] = min(value, cap)
            return clamped
    clamped["max_tokens"] = cap
    return clamped


def estimate_output_tokens_hint(extra: dict) -> int:
    """max_tokens/max_completion_tokens клиента, если задан и осмыслен —
    иначе консервативный дефолт (лучше зарезервировать с запасом и изредка
    отказать легитимному длинному ответу, чем недорезервировать).

    Умножается на n — число вариантов ответа. Оплачиваются ВСЕ варианты, а
    потолок длины (clamp_output_tokens) режет каждый по отдельности: без
    множителя клиент с n=10 резервировал бы десятую часть того, что спишется,
    и уводил баланс в минус ровно во столько же раз.
    """
    hint = extra.get("max_tokens") or extra.get("max_completion_tokens")
    if not (isinstance(hint, int) and hint > 0):
        hint = settings.default_max_output_tokens_estimate
    variants = extra.get("n")
    if isinstance(variants, int) and variants > 1:
        hint *= variants
    return hint


def estimate_call_cost_usd(price: ModelPrice | None, messages: list[dict], extra: dict) -> Decimal | None:
    """Оценка ДО вызова провайдера — для резерва (1.1 доработок). None, если
    прайса нет (тогда start_call падает обратно на простую проверку balance > 0)."""
    if price is None:
        return None
    usage = UsageAmounts(
        input_text_tokens=estimate_messages_tokens(messages),
        output_tokens=estimate_output_tokens_hint(extra),
    )
    return compute_cost(price, usage)
