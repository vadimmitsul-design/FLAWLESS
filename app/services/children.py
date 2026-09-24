"""Parent-owned child accounts and their usage history."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AccessDenied, EntityNotFound, InvalidInput, StateConflict
from app.core.security import hash_password, password_problem
from app.db.models import Customer, UsageEvent


@dataclass(frozen=True)
class ChildHistory:
    child: Customer
    events: Sequence[UsageEvent]


def require_parent(customer: Customer) -> None:
    if customer.is_child:
        raise AccessDenied("child accounts cannot create children")


async def create_child(
    session: AsyncSession,
    parent: Customer,
    email: str,
    name: str,
    password: str,
) -> Customer:
    require_parent(parent)
    problem = password_problem(password)
    if problem is not None:
        raise InvalidInput(problem.capitalize() + ".")
    email = email.strip().lower()
    existing = await session.scalar(select(Customer).where(Customer.email == email))
    if existing is not None:
        raise StateConflict("Этот email уже зарегистрирован")
    child = Customer(
        email=email,
        name=name.strip(),
        password_hash=hash_password(password),
        is_child=True,
        parent_customer_id=parent.id,
    )
    session.add(child)
    return child


async def child_history(session: AsyncSession, parent_id: int, child_id: int) -> ChildHistory:
    child = await session.get(Customer, child_id)
    if child is None or child.parent_customer_id != parent_id:
        raise EntityNotFound("Not Found")
    events = (
        await session.scalars(
            select(UsageEvent)
            .where(UsageEvent.customer_id == child.id)
            .order_by(UsageEvent.created_at.desc())
            .limit(50)
        )
    ).all()
    return ChildHistory(child, events)
