"""Subscription catalogue, order lookup and purchase operations."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound
from app.db.models import Customer, Product, SubscriptionOrder
from app.services import billing


@dataclass(frozen=True)
class ShopPage:
    products: Sequence[Product]
    orders: Sequence[tuple[SubscriptionOrder, Product]]


async def shop_page(session: AsyncSession, customer_id: int) -> ShopPage:
    products = (
        await session.scalars(select(Product).where(Product.active).order_by(Product.price_rub))
    ).all()
    orders = (
        (
            await session.execute(
                select(SubscriptionOrder, Product)
                .join(Product, Product.id == SubscriptionOrder.product_id)
                .where(SubscriptionOrder.customer_id == customer_id)
                .order_by(SubscriptionOrder.created_at.desc())
            )
        )
        .tuples()
        .all()
    )
    return ShopPage(products, orders)


async def purchase(
    session: AsyncSession,
    customer_id: int,
    product_id: int,
    account_email: str,
    note: str,
) -> SubscriptionOrder:
    product = await session.get(Product, product_id)
    if product is None or not product.active:
        raise EntityNotFound("Not Found")
    return await billing.purchase_subscription(
        session,
        customer_id,
        product,
        account_email.strip(),
        note.strip() or None,
    )


async def all_orders(
    session: AsyncSession,
) -> Sequence[tuple[SubscriptionOrder, Product, Customer]]:
    return (
        (
            await session.execute(
                select(SubscriptionOrder, Product, Customer)
                .join(Product, Product.id == SubscriptionOrder.product_id)
                .join(Customer, Customer.id == SubscriptionOrder.customer_id)
                .order_by(SubscriptionOrder.status != "paid", SubscriptionOrder.created_at.desc())
            )
        )
        .tuples()
        .all()
    )


async def paid_order(session: AsyncSession, order_id: int) -> SubscriptionOrder:
    order = await session.get(SubscriptionOrder, order_id)
    if order is None or order.status != "paid":
        raise EntityNotFound("Not Found")
    return order
