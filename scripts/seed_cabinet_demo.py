"""Create synthetic cabinet data in one fixed local SQLite database.

No production configuration is imported: the working directory and database
environment are isolated before application modules are loaded. Repeated runs
preserve the existing demo database and any changes made in the preview.
"""

import asyncio
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "dist" / "cabinet-preview"
DATABASE = PREVIEW / "cabinet.sqlite3"
EMAIL = "demo@flawless.local"
PASSWORD = "Neon-Demo-2026!"

# Local fixture prices; no claim that these are current provider quotations.
MODELS = [
    ("gpt-5-mini", "openai/gpt-5-mini", "0.25", "2.00"),
    ("claude-sonnet", "anthropic/claude-sonnet-5", "2.00", "10.00"),
    ("gemini-flash", "google/gemini-3.5-flash", "1.50", "9.00"),
    ("gemini-flash-lite", "google/gemini-3.1-flash-lite", "0.25", "1.50"),
]


def prepare_environment() -> None:
    """Never select a database or .env file from the caller's environment."""
    PREVIEW.mkdir(parents=True, exist_ok=True)
    if PREVIEW.resolve() != ROOT.resolve() / "dist" / "cabinet-preview":
        raise RuntimeError("Preview path must stay inside the repository")
    if DATABASE.is_symlink() or (PREVIEW / ".env").exists():
        raise RuntimeError("Preview database must not be a link or use an .env file")
    os.chdir(PREVIEW)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DATABASE.as_posix()}"
    os.environ["SESSION_SECRET"] = "local-seed-only-" + secrets.token_urlsafe(32)
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    sys.path.insert(0, str(ROOT))


def write_model_fixture() -> None:
    import yaml

    # The explicit loopback endpoint prevents demo chat from calling any provider.
    config = {
        "model_list": [
            {
                "model_name": alias,
                "litellm_params": {
                    "model": f"openrouter/{model}",
                    "api_key": "local-preview-no-provider-access",
                    "api_base": "http://127.0.0.1:9/v1",
                    "timeout": 2,
                    "max_retries": 0,
                },
            }
            for alias, model, _, _ in MODELS
        ]
    }
    (PREVIEW / "models.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


async def seed() -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.security import generate_api_key, hash_api_key, hash_password
    from app.db.models import (
        ApiKey,
        Base,
        Customer,
        ModelPrice,
        PricingConfig,
        TopupRequest,
        UsageEvent,
        WalletLedger,
        WebConversation,
        WebMessage,
    )

    if DATABASE.exists():
        print(f"Existing local preview preserved: {DATABASE}")
        return

    engine = create_async_engine(os.environ["DATABASE_URL"])
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    opening_balance = Decimal("15000.00")
    total_spent = Decimal(0)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            customer = Customer(
                email=EMAIL,
                name="Денис",
                password_hash=hash_password(PASSWORD),
                balance_rub=opening_balance,
                monthly_limit_rub=Decimal("10000"),
                daily_limit_rub=Decimal("1500"),
                created_at=now - timedelta(days=42),
            )
            session.add(customer)
            await session.flush()
            session.add(PricingConfig(id=1, markup_percent=30, usd_rub_rate=95))
            prices = []
            for _, model, input_price, output_price in MODELS:
                price = ModelPrice(
                    provider="openrouter",
                    model=model,
                    price_per_1m_input_tokens=Decimal(input_price),
                    price_per_1m_output_tokens=Decimal(output_price),
                    valid_from=now - timedelta(days=60),
                )
                session.add(price)
                prices.append(price)
            keys = []
            for index, name in enumerate(["Studio workspace", "Playground"]):
                raw_key = generate_api_key()
                key = ApiKey(
                    customer_id=customer.id,
                    name=name,
                    key_hash=hash_api_key(raw_key),
                    last_four=raw_key[-4:],
                    daily_limit_rub=Decimal("500") if index else None,
                    created_at=now - timedelta(days=20 - index * 10),
                )
                session.add(key)
                keys.append(key)
            topup = TopupRequest(
                customer_id=customer.id,
                amount_rub=opening_balance,
                status="confirmed",
                note="Демонстрационное пополнение",
                created_at=now - timedelta(days=10),
                decided_at=now - timedelta(days=10),
            )
            session.add(topup)
            await session.flush()
            session.add(
                WalletLedger(
                    customer_id=customer.id,
                    entry_type="topup",
                    delta_rub=opening_balance,
                    topup_request_id=topup.id,
                    note="Synthetic local preview balance",
                    created_at=topup.created_at,
                )
            )

            for day_index, calls in enumerate([43, 58, 49, 76, 62, 95, 84]):
                day = today - timedelta(days=6 - day_index)
                available_seconds = min(86400, max(1, int((now - day).total_seconds())))
                for index in range(calls):
                    model_index = (index + day_index) % len(MODELS)
                    _, model, input_rate, output_rate = MODELS[model_index]
                    created_at = day + timedelta(seconds=available_seconds * index // calls)
                    input_tokens = 1450 + index * 107
                    output_tokens = 420 + index * 29
                    cost = (
                        (input_tokens * Decimal(input_rate) + output_tokens * Decimal(output_rate))
                        / Decimal(1_000_000)
                    ).quantize(Decimal("0.000001"))
                    charge = (cost * Decimal("123.5")).quantize(Decimal("0.0001"))
                    event = UsageEvent(
                        customer_id=customer.id,
                        billing_customer_id=customer.id,
                        api_key_id=keys[index % 2].id,
                        provider="openrouter",
                        model=model,
                        price_id=prices[model_index].id,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost,
                        charged_rub=charge,
                        markup_percent=30,
                        usd_rub_rate=95,
                        status="success",
                        latency_ms=460 + index * 31,
                        created_at=created_at,
                        completed_at=created_at + timedelta(seconds=2),
                    )
                    session.add(event)
                    await session.flush()
                    session.add(
                        WalletLedger(
                            customer_id=customer.id,
                            entry_type="usage",
                            delta_rub=-charge,
                            usage_event_id=event.id,
                            created_at=created_at,
                        )
                    )
                    total_spent += charge
            customer.balance_rub = opening_balance - total_spent

            examples = [
                (
                    "Стратегия запуска продукта",
                    "claude-sonnet",
                    "Помоги составить план запуска нового цифрового продукта.",
                    "Начнём с трёх этапов: исследование аудитории, проверка гипотезы и запуск. "
                    "Для первой недели определите один сегмент клиентов, проведите пять интервью "
                    "и выберите метрику, которая покажет пользу продукта.",
                ),
                (
                    "Архитектура платёжного сервиса",
                    "gpt-5-mini",
                    "Как сделать обработку платежей устойчивой к повторным запросам?",
                    "Используйте ключ идемпотентности и уникальное ограничение в базе. "
                    "Запись платежа и изменение баланса должны быть одной транзакцией. "
                    "Повторный запрос возвращает сохранённый результат первой операции.",
                ),
                (
                    "Идеи для нового бренда",
                    "gemini-flash",
                    "Предложи направление для визуального языка технологического бренда.",
                    "Попробуйте графитовый фон, кислотный лайм как главный акцент и "
                    "крупную типографику. Геометрия интерфейса может напоминать архитектуру "
                    "ночного города, а цвет должен выделять действия и состояние системы.",
                ),
            ]
            for index, (title, alias, question, answer) in enumerate(examples):
                created_at = now - timedelta(hours=index * 8 + 1)
                conversation = WebConversation(
                    customer_id=customer.id,
                    title=title,
                    model_alias=alias,
                    created_at=created_at,
                    updated_at=created_at + timedelta(minutes=3),
                )
                session.add(conversation)
                await session.flush()
                session.add_all(
                    [
                        WebMessage(
                            conversation_id=conversation.id,
                            role=role,
                            content=content,
                            created_at=created_at + timedelta(minutes=position),
                        )
                        for position, (role, content) in enumerate(
                            [("user", question), ("assistant", answer)]
                        )
                    ]
                )
            await session.flush()
            ledger_balance = await session.scalar(select(func.sum(WalletLedger.delta_rub)))
            if ledger_balance != customer.balance_rub:
                raise RuntimeError("Demo ledger does not match the customer balance")
            await session.commit()
            print(f"Created local demo: 467 requests, balance {customer.balance_rub:.2f} RUB")
    finally:
        await engine.dispose()


def main() -> None:
    prepare_environment()
    write_model_fixture()
    asyncio.run(seed())
    print(f"Local preview login: {EMAIL} / {PASSWORD}")
    print("Provider calls are disabled; this database contains synthetic data only.")


if __name__ == "__main__":
    main()
