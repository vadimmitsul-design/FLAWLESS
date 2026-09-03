"""Сид каталога подписок для платёжного агента.

Запуск: docker compose exec neurohub python scripts/seed_products.py
Повторный запуск безопасен — существующие по имени строки не трогает.

ВНИМАНИЕ: цены ориентировочные (курс ~95 + запас на комиссию карты-донора),
сверить с реальной стоимостью оплаты через брокера перед продом.
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Product

PRODUCTS = [
    ("ChatGPT Plus", "Подписка OpenAI ChatGPT Plus на 1 месяц", "2400.00"),
    ("Claude Pro", "Подписка Anthropic Claude Pro на 1 месяц", "2400.00"),
    ("Midjourney Standard", "Подписка Midjourney Standard на 1 месяц", "3200.00"),
]


async def main() -> None:
    async with SessionLocal() as session:
        for name, description, price in PRODUCTS:
            exists = (
                await session.execute(select(Product).where(Product.name == name))
            ).scalar_one_or_none()
            if exists is None:
                session.add(Product(name=name, description=description, price_rub=Decimal(price)))
                print(f"created product '{name}' ({price} руб.)")
        await session.commit()
        print("seed done")


if __name__ == "__main__":
    asyncio.run(main())
