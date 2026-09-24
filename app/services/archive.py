"""Hash-based dialogue archive and public verification."""

import hashlib
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, DialogueArchive


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def customer_archives(session: AsyncSession, customer_id: int) -> Sequence[DialogueArchive]:
    return (
        await session.scalars(
            select(DialogueArchive)
            .where(DialogueArchive.customer_id == customer_id)
            .order_by(DialogueArchive.created_at.desc())
        )
    ).all()


async def create_archive(session: AsyncSession, customer_id: int, content: str, label: str) -> bool:
    """Stage a new hash record; return False when that content already exists."""
    digest = content_hash(content)
    existing = await session.scalar(
        select(DialogueArchive).where(DialogueArchive.content_hash == digest)
    )
    if existing is not None:
        return False
    session.add(
        DialogueArchive(customer_id=customer_id, content_hash=digest, label=label.strip() or None)
    )
    return True


async def verify(session: AsyncSession, content: str) -> tuple[DialogueArchive, Customer] | None:
    return (
        (
            await session.execute(
                select(DialogueArchive, Customer)
                .join(Customer, Customer.id == DialogueArchive.customer_id)
                .where(DialogueArchive.content_hash == content_hash(content))
            )
        )
        .tuples()
        .first()
    )
