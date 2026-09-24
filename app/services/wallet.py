"""Customer wallet requests; financial settlement belongs to billing."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound, InvalidInput, StateConflict
from app.db.models import Customer, TopupRequest
from app.services import billing


async def request_topup(
    session: AsyncSession, customer_id: int, amount_rub: Decimal, note: str
) -> None:
    if amount_rub <= 0:
        raise InvalidInput("amount_rub must be positive")
    session.add(
        TopupRequest(customer_id=customer_id, amount_rub=amount_rub, note=note.strip() or None)
    )
    await session.flush()


async def topup_requests(session: AsyncSession) -> list[tuple[TopupRequest, Customer]]:
    rows = await session.execute(
        select(TopupRequest, Customer)
        .join(Customer, Customer.id == TopupRequest.customer_id)
        .order_by(TopupRequest.status != "requested", TopupRequest.created_at.desc())
    )
    return [(request, customer) for request, customer in rows]


async def _requested_topup(session: AsyncSession, topup_id: int) -> TopupRequest:
    topup = await session.get(TopupRequest, topup_id)
    if topup is None:
        raise EntityNotFound("Not Found")
    if topup.status != "requested":
        raise StateConflict("по заявке уже принято решение")
    return topup


async def confirm_topup(session: AsyncSession, topup_id: int, admin_id: int) -> None:
    """Complete one financial operation through billing's transaction facade."""
    topup = await _requested_topup(session, topup_id)
    if await billing.confirm_topup(session, topup, admin_id=admin_id) is None:
        raise StateConflict("по заявке уже принято решение")


async def reject_topup(session: AsyncSession, topup_id: int, admin_id: int) -> None:
    topup = await _requested_topup(session, topup_id)
    if not await billing.reject_topup(session, topup, admin_id=admin_id):
        raise StateConflict("по заявке уже принято решение")


async def adjust_balance(
    session: AsyncSession,
    customer_id: int,
    *,
    amount: Decimal,
    entry_type: str,
    note: str,
    admin_id: int,
) -> None:
    if entry_type not in ("topup", "adjustment"):
        raise InvalidInput("entry_type must be topup or adjustment")
    if amount == 0:
        raise InvalidInput("amount must not be zero")
    if not note.strip():
        raise InvalidInput("note is required for manual balance changes")
    if await session.get(Customer, customer_id) is None:
        raise EntityNotFound("Not Found")
    await billing.admin_adjust_balance(
        session,
        customer_id=customer_id,
        delta_rub=amount,
        entry_type=entry_type,
        note=note.strip(),
        admin_id=admin_id,
    )
