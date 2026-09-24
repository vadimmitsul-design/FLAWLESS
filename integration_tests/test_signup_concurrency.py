"""Database invariants for simultaneous account registrations."""

import asyncio

from pydantic_settings import SettingsConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import InvalidInput, StateConflict
from app.db.models import Customer, InviteCode
from app.services.auth import register_customer

Sessions = async_sessionmaker[AsyncSession]


class RegistrationSettings(Settings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


def test_same_email_registers_one_customer(sessions: Sessions) -> None:
    async def run() -> None:
        barrier = asyncio.Barrier(2)
        config = RegistrationSettings(signup_mode="open", signup_allowed_email_domains="")

        async def register() -> str:
            try:
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT 1"))
                    await barrier.wait()
                    await register_customer(
                        session,
                        email="duplicate@integration.invalid",
                        name="Test",
                        password="IntegrationPass123",
                        invite="",
                        config=config,
                    )
            except (StateConflict, IntegrityError):
                return "conflict"
            return "created"

        results = await asyncio.wait_for(asyncio.gather(register(), register()), timeout=15)
        assert sorted(results) == ["conflict", "created"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Customer)) == 1

    asyncio.run(run())


def test_same_invitation_creates_only_one_customer(sessions: Sessions) -> None:
    async def run() -> None:
        async with sessions() as session, session.begin():
            admin = Customer(
                email="admin@integration.invalid",
                name="Admin",
                password_hash="unused",
                role="admin",
            )
            session.add(admin)
            await session.flush()
            session.add(InviteCode(code="one-use-integration", created_by_admin_id=admin.id))
        barrier = asyncio.Barrier(2)
        config = RegistrationSettings(signup_mode="invite", signup_allowed_email_domains="")

        async def register(email: str) -> str:
            try:
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT 1"))
                    await barrier.wait()
                    await register_customer(
                        session,
                        email=email,
                        name="Test",
                        password="IntegrationPass123",
                        invite="one-use-integration",
                        config=config,
                    )
            except (InvalidInput, StateConflict):
                return "conflict"
            return "created"

        results = await asyncio.wait_for(
            asyncio.gather(
                register("first@integration.invalid"), register("second@integration.invalid")
            ),
            timeout=15,
        )
        assert sorted(results) == ["conflict", "created"]
        async with sessions() as session:
            customers = list(
                (await session.scalars(select(Customer).where(Customer.role == "customer"))).all()
            )
            assert len(customers) == 1
            invitation = await session.scalar(select(InviteCode))
            assert invitation is not None
            assert invitation.used_by_customer_id == customers[0].id
            assert invitation.used_at is not None

    asyncio.run(run())
