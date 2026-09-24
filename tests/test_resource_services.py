"""Resource operations participate in caller transactions and enforce rules."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import InvalidInput
from app.db import SessionLocal
from app.db.models import Customer, Resource, ResourcePayment, ResourceRequest
from app.services import resources


def test_fulfilment_rolls_back_with_the_callers_transaction():
    async def run():
        async with SessionLocal() as session:
            admin_id = await session.scalar(select(Customer.id).where(Customer.role == "admin"))
            assert admin_id is not None
            request = ResourceRequest(
                customer_id=admin_id,
                kind="subscription",
                name=f"rollback-{uuid4()}",
            )
            session.add(request)
            await session.flush()

            with pytest.raises(RuntimeError, match="operation failed after fulfilment"):
                async with session.begin_nested():
                    resource = await resources.fulfil_request(
                        session,
                        request.id,
                        resources.PaymentDetails(
                            amount=Decimal("100"),
                            currency="RUB",
                            period_end=datetime(2030, 1, 1, tzinfo=UTC),
                        ),
                        admin_id=admin_id,
                        url="",
                    )
                    resource_id = resource.id
                    raise RuntimeError("operation failed after fulfilment")

            await session.refresh(request)
            assert request.status == "requested"
            assert request.resource_id is None
            assert await session.get(Resource, resource_id) is None
            assert (
                await session.scalar(
                    select(func.count(ResourcePayment.id)).where(
                        ResourcePayment.resource_id == resource_id
                    )
                )
                == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity"])
def test_requested_cost_must_be_finite_and_positive(amount):
    async def run():
        async with SessionLocal() as session:
            admin_id = await session.scalar(select(Customer.id).where(Customer.role == "admin"))
            assert admin_id is not None
            with pytest.raises(InvalidInput, match="сумма должна быть больше нуля"):
                await resources.request_resource(
                    session,
                    admin_id,
                    resources.RequestDetails(
                        kind="subscription",
                        name="Example",
                        estimated_amount=Decimal(amount),
                    ),
                )

    asyncio.run(run())
