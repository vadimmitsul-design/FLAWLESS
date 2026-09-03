"""Тестовая БД — один общий SQLite-файл на весь прогон pytest (не Postgres):
проще и быстрее для CI, покрывает всю бизнес-логику приложения. Переменные
окружения выставляются ДО первого импорта app.* — Settings() и engine
создаются один раз при импорте модуля и кэшируются.

Изоляция между тестами — через уникальные email в каждом тесте, а не через
отдельную БД на тест (см. README-комментарий в test_api_flow.py).
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_DIR.parent
DB_FILE = TEST_DIR / "_test.db"

if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
os.environ["SESSION_SECRET"] = "test-secret"
# Не подхватывать боевой токен из .env: он реально ходит в Telegram API из
# lifespan() при каждом `with TestClient(app)`. Фиктивный, но непустой токен
# нужен, чтобы /telegram/link не отдавал 404 (роут требует settings.telegram_bot_token).
os.environ["TELEGRAM_BOT_TOKEN"] = "test:dummy-token"

sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


@pytest.fixture(scope="session", autouse=True)
def _database():
    from app.db import engine
    from app.models import Base

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield
    asyncio.run(engine.dispose())  # освободить файловые хендлы SQLite перед удалением
    if DB_FILE.exists():
        DB_FILE.unlink()


@pytest.fixture(scope="session", autouse=True)
def _admin(_database):
    from app.db import SessionLocal
    from app.models import Customer
    from app.security import hash_password

    async def _create():
        async with SessionLocal() as session:
            session.add(
                Customer(
                    email=ADMIN_EMAIL,
                    name="Admin",
                    password_hash=hash_password(ADMIN_PASSWORD),
                    role="admin",
                )
            )
            await session.commit()

    asyncio.run(_create())


@pytest.fixture(scope="session", autouse=True)
def _seed_prices(_database):
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import ModelPrice, PricingConfig

    async def _create():
        async with SessionLocal() as session:
            session.add(
                ModelPrice(
                    provider="openai",
                    model="gpt-5-mini",
                    price_per_1m_input_tokens=Decimal("0.25"),
                    price_per_1m_output_tokens=Decimal("1.00"),
                    valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
            )
            session.add(PricingConfig(id=1, markup_percent=Decimal("30.00"), usd_rub_rate=Decimal("95.0000")))
            await session.commit()

    asyncio.run(_create())


@pytest.fixture(scope="session", autouse=True)
def _seed_products(_database):
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import Product

    async def _create():
        async with SessionLocal() as session:
            session.add(Product(name="ChatGPT Plus", description="Тестовый товар", price_rub=Decimal("2400.00")))
            await session.commit()

    asyncio.run(_create())


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """ratelimit._login_hits/_hits — модульные глобальные счётчики (сознательно,
    см. app/ratelimit.py), в проде это верно, но в тестах все запросы идут с
    одного синтетического IP TestClient — без сброса лимит на /login исчерпывается
    заявками совершенно не связанных тестов. Сбрасываем перед каждым тестом."""
    from app import ratelimit

    ratelimit._hits.clear()
    ratelimit._login_hits.clear()


@pytest.fixture(scope="session", autouse=True)
def _no_real_telegram_polling():
    """lifespan() запускает telegram_bot.poll_loop() фоновой таской при каждом
    `with TestClient(app)` (см. TELEGRAM_BOT_TOKEN выше) — подменяем на
    бесконечный sleep, чтобы тесты не долбили api.telegram.org."""
    from app import telegram_bot

    async def _noop_poll_loop():
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    telegram_bot.poll_loop = _noop_poll_loop


@pytest.fixture()
def client():
    """Свежий TestClient (== своя cookie-сессия) на каждый тест."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c
