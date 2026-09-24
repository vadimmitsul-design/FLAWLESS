"""Wallet reconciliation uses exact stored amounts and never repairs balances."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db import SessionLocal
from app.db.models import Customer, UsageEvent, WalletLedger
from app.services.wallet_reconciliation import WalletMismatch, find_wallet_mismatches
from app.workers import alerts


@pytest.mark.parametrize(
    ("balance", "entries", "difference"),
    [
        ("0", [], None),
        ("10", [], "10"),
        ("7.5000", ["10", "-2.5"], None),
        ("0.3", ["0.1", "0.2"], None),
        ("7.4999", ["10", "-2.5"], "-0.0001"),
        ("7.5001", ["10", "-2.5"], "0.0001"),
    ],
)
def test_wallet_reconciliation(balance, entries, difference):
    async def run():
        async with SessionLocal() as session:
            customer = Customer(
                email=f"reconcile-{uuid4()}@test.local",
                name="Reconciliation",
                password_hash="unused",
                balance_rub=Decimal(balance),
                active=False,
            )
            session.add(customer)
            await session.flush()
            session.add_all(
                WalletLedger(
                    customer_id=customer.id,
                    entry_type="adjustment",
                    delta_rub=Decimal(delta),
                )
                for delta in entries
            )
            await session.flush()

            mismatches = {item.customer_id: item for item in await find_wallet_mismatches(session)}
            if difference is None:
                assert customer.id not in mismatches
            else:
                item = mismatches[customer.id]
                assert item.balance_rub == Decimal(balance)
                assert item.difference_rub == Decimal(difference)

            await session.refresh(customer)
            assert customer.balance_rub == Decimal(balance)
            # Context exit rolls back every fixture row: no cross-test damage.

    asyncio.run(run())


def test_pending_reservation_does_not_change_ledger_balance():
    async def run():
        async with SessionLocal() as session:
            customer = Customer(
                email=f"reconcile-reserve-{uuid4()}@test.local",
                name="Reserved wallet",
                password_hash="unused",
                balance_rub=Decimal("10"),
            )
            session.add(customer)
            await session.flush()
            session.add(
                WalletLedger(
                    customer_id=customer.id,
                    entry_type="topup",
                    delta_rub=Decimal("10"),
                )
            )
            session.add(
                UsageEvent(
                    customer_id=customer.id,
                    billing_customer_id=customer.id,
                    provider="test",
                    model="test",
                    status="pending",
                    reserved_rub=Decimal("8"),
                )
            )
            await session.flush()
            assert all(
                item.customer_id != customer.id for item in await find_wallet_mismatches(session)
            )

    asyncio.run(run())


def test_wallet_alert_is_bounded_and_suppressed_during_cooldown(monkeypatch, caplog):
    mismatches = [
        WalletMismatch(customer_id=i, balance_rub=Decimal("1"), ledger_rub=Decimal("0"))
        for i in range(101, 113)
    ]
    monkeypatch.setattr(alerts, "find_wallet_mismatches", AsyncMock(return_value=mismatches))
    monkeypatch.setattr(alerts.settings, "telegram_bot_token", "")
    monkeypatch.setattr(alerts.settings, "enable_resources", False)
    monkeypatch.setattr(alerts, "default_cooldown", alerts.AlertCooldown())

    async def run():
        async with SessionLocal() as session:
            problems = await alerts.collect_problems(session)
            text = dict(problems)["wallet_reconciliation"]
            assert "12" in text
            assert "#101:" in text
            assert "#110:" in text
            assert "#111:" not in text
            await alerts.check_and_notify(session)
            await alerts.check_and_notify(session)

    asyncio.run(run())
    messages = [record.message for record in caplog.records if "wallet_ledger" in record.message]
    assert len(messages) == 1
