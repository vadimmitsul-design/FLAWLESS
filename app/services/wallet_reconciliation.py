"""Read-only reconciliation of materialized wallet balances and the ledger."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Numeric, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, WalletLedger


@dataclass(frozen=True, slots=True)
class WalletMismatch:
    customer_id: int
    balance_rub: Decimal
    ledger_rub: Decimal

    @property
    def difference_rub(self) -> Decimal:
        return self.balance_rub - self.ledger_rub


async def find_wallet_mismatches(session: AsyncSession) -> list[WalletMismatch]:
    """Compare both values in one database snapshot without changing money.

    Reservations affect available funds, not the materialized balance, so they
    are deliberately excluded. Customers without ledger entries have a ledger
    balance of zero. PostgreSQL NUMERIC comparisons preserve all four decimals.
    """
    # Stored amounts have scale 4. ROUND also prevents SQLite's floating-point
    # SUM from reporting a mismatch for, for example, 0.1 + 0.2 in local tests.
    ledger_rub = func.round(
        func.coalesce(func.sum(WalletLedger.delta_rub), Decimal("0")),
        4,
        type_=Numeric(14, 4),
    )
    rows = await session.execute(
        select(Customer.id, Customer.balance_rub, ledger_rub)
        .outerjoin(WalletLedger, WalletLedger.customer_id == Customer.id)
        .group_by(Customer.id, Customer.balance_rub)
        .having(Customer.balance_rub != ledger_rub)
        .order_by(Customer.id)
    )
    return [
        WalletMismatch(customer_id=customer_id, balance_rub=balance, ledger_rub=ledger)
        for customer_id, balance, ledger in rows
    ]
