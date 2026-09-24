"""Prompt marketplace queries and publication rules."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AccessDenied, EntityNotFound, InvalidInput
from app.db.models import Customer, Prompt


@dataclass(frozen=True)
class PromptPage:
    marketplace: Sequence[tuple[Prompt, Customer]]
    my_prompts: Sequence[Prompt]


async def active_prompt(session: AsyncSession, prompt_id: int, *, enabled: bool) -> Prompt:
    prompt = await session.get(Prompt, prompt_id) if enabled else None
    if prompt is None or not prompt.active:
        raise EntityNotFound(f"unknown prompt_id {prompt_id}")
    return prompt


async def prompt_page(session: AsyncSession, customer_id: int) -> PromptPage:
    marketplace = (
        (
            await session.execute(
                select(Prompt, Customer)
                .join(Customer, Customer.id == Prompt.author_customer_id)
                .where(Prompt.active)
                .order_by(Prompt.created_at.desc())
            )
        )
        .tuples()
        .all()
    )
    mine = (
        await session.scalars(
            select(Prompt)
            .where(Prompt.author_customer_id == customer_id)
            .order_by(Prompt.created_at.desc())
        )
    ).all()
    return PromptPage(marketplace, mine)


def create_prompt(
    session: AsyncSession,
    customer: Customer,
    title: str,
    description: str,
    system_prompt: str,
    price_rub: Decimal,
) -> Prompt:
    if customer.is_child:
        raise AccessDenied("детский аккаунт не может публиковать промпты")
    if price_rub <= 0:
        raise InvalidInput("price_rub must be positive")
    prompt = Prompt(
        author_customer_id=customer.id,
        title=title.strip(),
        description=description.strip() or None,
        system_prompt=system_prompt.strip(),
        price_rub=price_rub,
    )
    session.add(prompt)
    return prompt
