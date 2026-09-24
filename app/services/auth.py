"""Account registration and credential workflows without HTTP responses."""

import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, allowed_signup_domains, email_domain_allowed
from app.core.errors import EntityNotFound, InvalidInput, StateConflict
from app.core.security import (
    generate_temp_password,
    hash_password,
    password_problem,
    verify_password,
)
from app.db.models import Customer, InviteCode, PasswordResetRequest, TelegramLinkCode, utcnow


async def register_customer(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    password: str,
    invite: str,
    config: Settings,
) -> Customer:
    """Stage the account and consume its invitation in the same transaction."""
    problem = password_problem(password)
    if problem is not None:
        raise InvalidInput(problem.capitalize() + ".")

    invitation = None
    if config.signup_mode == "invite":
        code = invite.strip()
        if not code:
            raise InvalidInput("Нужен код приглашения — запросите его у администратора")
        invitation = await session.scalar(
            select(InviteCode).where(
                InviteCode.code == code, InviteCode.used_by_customer_id.is_(None)
            )
        )
        if invitation is None:
            raise InvalidInput("Код приглашения не найден или уже использован")

    email = email.strip().lower()
    if not email_domain_allowed(email, config.signup_allowed_email_domains):
        domains = allowed_signup_domains(config.signup_allowed_email_domains)
        raise InvalidInput(
            "Зарегистрироваться можно только с рабочей почтой: "
            + ", ".join("@" + domain for domain in domains)
        )
    if await session.scalar(select(Customer.id).where(Customer.email == email)) is not None:
        raise StateConflict("Этот email уже зарегистрирован")

    try:
        async with session.begin_nested():
            customer = Customer(
                email=email, name=name.strip(), password_hash=hash_password(password)
            )
            session.add(customer)
            await session.flush()
            if invitation is not None:
                consumed = await session.scalar(
                    update(InviteCode)
                    .where(InviteCode.id == invitation.id, InviteCode.used_by_customer_id.is_(None))
                    .values(used_by_customer_id=customer.id, used_at=utcnow())
                    .returning(InviteCode.id)
                )
                if consumed is None:
                    raise StateConflict("Код приглашения только что использовали — запросите новый")
    except IntegrityError as exc:
        if await session.scalar(select(Customer.id).where(Customer.email == email)) is None:
            raise
        raise StateConflict("Этот email уже зарегистрирован") from exc

    return customer


async def authenticate(session: AsyncSession, email: str, password: str) -> Customer | None:
    customer = await session.scalar(
        select(Customer).where(Customer.email == email.strip().lower(), Customer.active)
    )
    if customer is None or not verify_password(password, customer.password_hash):
        return None
    return customer


async def request_password_reset(session: AsyncSession, email: str) -> None:
    """Stage a request without disclosing whether the address is registered."""
    customer = await session.scalar(
        select(Customer).where(Customer.email == email.strip().lower(), Customer.active)
    )
    if customer is not None:
        session.add(PasswordResetRequest(customer_id=customer.id))
        await session.flush()


async def password_reset_requests(
    session: AsyncSession,
) -> list[tuple[PasswordResetRequest, Customer]]:
    rows = await session.execute(
        select(PasswordResetRequest, Customer)
        .join(Customer, Customer.id == PasswordResetRequest.customer_id)
        .order_by(
            PasswordResetRequest.status != "requested", PasswordResetRequest.created_at.desc()
        )
    )
    return [(request, customer) for request, customer in rows]


@dataclass(frozen=True, slots=True)
class ResetPassword:
    email: str
    password: str


async def complete_password_reset(
    session: AsyncSession, request_id: int, admin_id: int
) -> ResetPassword:
    request = await session.get(PasswordResetRequest, request_id, with_for_update=True)
    if request is None or request.status != "requested":
        raise EntityNotFound("Not Found")
    customer = await session.get(Customer, request.customer_id)
    if customer is None:
        raise EntityNotFound("Not Found")
    password = generate_temp_password()
    customer.password_hash = hash_password(password)
    request.status = "completed"
    request.completed_at = utcnow()
    request.completed_by_admin_id = admin_id
    await session.flush()
    return ResetPassword(email=customer.email, password=password)


async def list_invitations(session: AsyncSession) -> list[tuple[InviteCode, Customer | None]]:
    rows = await session.execute(
        select(InviteCode, Customer)
        .outerjoin(Customer, Customer.id == InviteCode.used_by_customer_id)
        .order_by(InviteCode.used_at.is_not(None), InviteCode.created_at.desc())
    )
    return [(invitation, customer) for invitation, customer in rows]


async def create_invitation(session: AsyncSession, admin_id: int, note: str) -> None:
    session.add(
        InviteCode(
            code=secrets.token_urlsafe(8),
            note=note.strip() or None,
            created_by_admin_id=admin_id,
        )
    )
    await session.flush()


async def create_telegram_link_code(session: AsyncSession, customer_id: int) -> str:
    code = secrets.token_hex(4)
    session.add(TelegramLinkCode(customer_id=customer_id, code=code))
    await session.flush()
    return code
