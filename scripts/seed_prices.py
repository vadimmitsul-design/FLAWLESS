"""Сид: прайс моделей из config/models.yaml + наценка/курс.

Запуск: docker compose exec neurohub python scripts/seed_prices.py
Повторный запуск безопасен — существующие строки не трогает.

Цены сверены с каталогом OpenRouter 2026-09-11. Держать их в актуальном
состоянии должен scripts/sync_openrouter_prices.py, а не ручная правка:
он закрывает устаревшую строку (valid_until) и заводит новую, не трогая
историю вызовов.
markup_percent/usd_rub_rate — тоже ориентировочные, править через
/admin (когда появится) или напрямую в pricing_config.
"""

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.db.models import ModelPrice, PricingConfig

PRICES_VALID_FROM = datetime(2026, 9, 1, tzinfo=UTC)

# provider, model, in_per_1m, out_per_1m (USD, себестоимость без наценки).
# СВЕРЕНО с каталогом OpenRouter 2026-09-11 скриптом sync_openrouter_prices.py.
# Не править на глаз: цены у провайдеров меняются, для этого есть скрипт.
PRICE_ROWS = [
    ("openrouter", "openai/gpt-5-mini", "0.25", "2.00"),
    ("openrouter", "anthropic/claude-sonnet-5", "2.00", "10.00"),
    ("openrouter", "google/gemini-3.5-flash", "1.50", "9.00"),
    ("openrouter", "google/gemini-3.1-flash-lite", "0.25", "1.50"),
]

DEFAULT_MARKUP_PERCENT = Decimal("30.00")
DEFAULT_USD_RUB_RATE = Decimal("95.0000")


async def main() -> None:
    async with SessionLocal() as session:
        for provider, model, in_rate, out_rate in PRICE_ROWS:
            exists = (
                (
                    await session.execute(
                        select(ModelPrice).where(
                            ModelPrice.provider == provider, ModelPrice.model == model
                        )
                    )
                )
                .scalars()
                .first()
            )
            if exists is None:
                session.add(
                    ModelPrice(
                        provider=provider,
                        model=model,
                        price_per_1m_input_tokens=Decimal(in_rate),
                        price_per_1m_output_tokens=Decimal(out_rate),
                        valid_from=PRICES_VALID_FROM,
                    )
                )
                print(f"created price {provider}/{model}")

        cfg = await session.get(PricingConfig, 1)
        if cfg is None:
            session.add(
                PricingConfig(
                    id=1, markup_percent=DEFAULT_MARKUP_PERCENT, usd_rub_rate=DEFAULT_USD_RUB_RATE
                )
            )
            print(
                f"created pricing_config: markup={DEFAULT_MARKUP_PERCENT}% rate={DEFAULT_USD_RUB_RATE}"
            )
        else:
            print("pricing_config already exists, unchanged")

        await session.commit()
        print("seed done")


if __name__ == "__main__":
    asyncio.run(main())
