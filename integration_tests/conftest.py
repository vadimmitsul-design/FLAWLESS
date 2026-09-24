"""PostgreSQL tests run separately from the legacy SQLite suite."""

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def postgres_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to the disposable PostgreSQL test database")
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or url.database != "flawless_test":
        raise pytest.UsageError(
            "Integration tests require postgresql+asyncpg and database flawless_test"
        )
    if url.host not in {"localhost", "127.0.0.1", "postgres"}:
        raise pytest.UsageError("Integration tests only accept the local test database")
    return value


@pytest.fixture(scope="session")
def migrated_database(postgres_url: str) -> str:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = postgres_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    return postgres_url


@pytest.fixture
def sessions(migrated_database: str) -> Iterator[async_sessionmaker[AsyncSession]]:
    # Tests use asyncio.run; NullPool prevents sharing asyncpg connections
    # between event loops. Concurrent operations still get separate connections.
    engine = create_async_engine(
        migrated_database,
        poolclass=NullPool,
        connect_args={"server_settings": {"statement_timeout": "10000"}},
    )

    async def clear_database() -> None:
        from app.db.models import Base

        preparer = engine.dialect.identifier_preparer
        tables = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    asyncio.run(clear_database())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())
