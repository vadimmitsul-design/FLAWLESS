"""Real transactions and locks, with no HTTP clients or provider calls."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Customer,
    PricingConfig,
    Product,
    Prompt,
    SubscriptionOrder,
    TopupRequest,
    UsageEvent,
    WalletLedger,
    WebConversation,
    WebMessage,
    utcnow,
)
from app.services import billing
from app.services.pricing import UsageAmounts
from app.workers import reaper

Sessions = async_sessionmaker[AsyncSession]


async def settle(
    session: AsyncSession,
    event: UsageEvent,
    *,
    prompt: Prompt | None = None,
    conversation_id: int | None = None,
    reply: str | None = None,
) -> Decimal:
    return await billing.finalize_success(
        session,
        event,
        usage=UsageAmounts(input_text_tokens=1, output_tokens=1),
        cost_usd=Decimal("1"),
        price_id=None,
        litellm_cost=None,
        pricing_cfg=PricingConfig(markup_percent=Decimal("0"), usd_rub_rate=Decimal("1")),
        latency_ms=1,
        provider_request_id=None,
        prompt=prompt,
        web_conversation_id=conversation_id,
        reply_text=reply,
    )


async def add_customer(sessions: Sessions, name: str, balance: str = "10") -> int:
    async with sessions() as session:
        customer = Customer(
            email=f"{name}@integration.invalid",
            name=name,
            password_hash="unused-in-integration-tests",
            balance_rub=Decimal(balance),
        )
        session.add(customer)
        await session.commit()
        return customer.id


async def reserve(
    sessions: Sessions, customer_id: int, barrier: asyncio.Barrier, key: str | None = None
) -> str:
    async with sessions() as session:
        await session.execute(text("SELECT 1"))
        await barrier.wait()
        try:
            await billing.start_call(
                session,
                customer_id,
                customer_id,
                "test",
                "test-model",
                estimated_reserve_rub=Decimal("8"),
                idempotency_key=key,
            )
        except billing.InsufficientBalance:
            return "insufficient"
        except IntegrityError:
            return "conflict"
        return "reserved"


def test_concurrent_reservations_cannot_exceed_balance(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "reserves")
        barrier = asyncio.Barrier(2)
        results = await asyncio.wait_for(
            asyncio.gather(
                reserve(sessions, customer_id, barrier),
                reserve(sessions, customer_id, barrier),
            ),
            timeout=15,
        )
        assert sorted(results) == ["insufficient", "reserved"]
        async with sessions() as session:
            pending = await session.scalar(
                select(func.sum(UsageEvent.reserved_rub)).where(UsageEvent.status == "pending")
            )
            assert pending == Decimal("8")

    asyncio.run(run())


def test_concurrent_idempotency_key_creates_one_event(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "idempotency", "100")
        barrier = asyncio.Barrier(2)
        results = await asyncio.wait_for(
            asyncio.gather(
                reserve(sessions, customer_id, barrier, "same-request"),
                reserve(sessions, customer_id, barrier, "same-request"),
            ),
            timeout=15,
        )
        assert sorted(results) == ["conflict", "reserved"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(UsageEvent)) == 1

    asyncio.run(run())


def test_concurrent_reservation_and_purchase_share_available_balance(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "purchase")
        async with sessions() as session:
            product = Product(name="Test", price_rub=Decimal("8"))
            session.add(product)
            await session.commit()
            product_id = product.id
        barrier = asyncio.Barrier(2)

        async def purchase() -> str:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                await barrier.wait()
                try:
                    await billing.purchase_subscription(
                        session, customer_id, product, "account@integration.invalid", None
                    )
                except billing.InsufficientBalance:
                    return "insufficient"
                return "reserved"

        results = await asyncio.wait_for(
            asyncio.gather(reserve(sessions, customer_id, barrier), purchase()), timeout=15
        )
        assert sorted(results) == ["insufficient", "reserved"]
        async with sessions() as session:
            customer = await session.get(Customer, customer_id)
            assert customer is not None
            assert await billing.available_balance(session, customer_id, customer.balance_rub) == 2

    asyncio.run(run())


def test_reaper_releases_only_expired_pending_reservations(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "reaper", "100")
        async with sessions() as session:
            old = await billing.start_call(
                session, customer_id, customer_id, "test", "test-model", Decimal("8")
            )
            old.created_at = utcnow() - timedelta(days=1)
            await session.commit()
            current = await billing.start_call(
                session, customer_id, customer_id, "test", "test-model", Decimal("8")
            )
            old_id, current_id = old.id, current.id
        async with sessions() as session:
            assert await reaper.reap_stale_pending_events(session) == 1
        async with sessions() as session:
            persisted_old = await session.get(UsageEvent, old_id)
            persisted_current = await session.get(UsageEvent, current_id)
            assert persisted_old is not None and persisted_old.status == "failed"
            assert persisted_current is not None and persisted_current.status == "pending"
            assert await billing.available_balance(session, customer_id, Decimal("100")) == 92

    asyncio.run(run())


def test_opposing_prompt_fees_complete_without_deadlock(sessions: Sessions) -> None:
    async def run() -> None:
        first = await add_customer(sessions, "first", "50")
        second = await add_customer(sessions, "second", "50")
        async with sessions() as session:
            first_prompt = Prompt(
                author_customer_id=first,
                title="First",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            second_prompt = Prompt(
                author_customer_id=second,
                title="Second",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            first_event = UsageEvent(
                customer_id=first, billing_customer_id=first, provider="test", model="test-model"
            )
            second_event = UsageEvent(
                customer_id=second, billing_customer_id=second, provider="test", model="test-model"
            )
            session.add_all([first_prompt, second_prompt, first_event, second_event])
            await session.commit()
            calls = [
                (first, second_prompt.id, first_event.id),
                (second, first_prompt.id, second_event.id),
            ]
        barrier = asyncio.Barrier(2)

        async def charge(customer_id: int, prompt_id: int, event_id: UUID) -> None:
            async with sessions() as session:
                prompt = await session.get(Prompt, prompt_id)
                assert prompt is not None
                await barrier.wait()
                await billing.charge_prompt_fee(session, customer_id, prompt, event_id)

        await asyncio.wait_for(asyncio.gather(*(charge(*call) for call in calls)), timeout=15)
        async with sessions() as session:
            balances = list((await session.scalars(select(Customer.balance_rub))).all())
            assert balances == [Decimal("45"), Decimal("45")]
            assert await session.scalar(select(func.count()).select_from(WalletLedger)) == 4

    asyncio.run(run())


def test_concurrent_finalization_charges_exactly_once(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "finalize")
        async with sessions() as session:
            event = await billing.start_call(
                session, customer_id, customer_id, "test", "test-model", Decimal("8")
            )
            event_id = event.id
        barrier = asyncio.Barrier(2)

        async def finalize() -> None:
            async with sessions() as session:
                event = await session.get(UsageEvent, event_id)
                assert event is not None
                await barrier.wait()
                await settle(session, event)

        await asyncio.wait_for(asyncio.gather(finalize(), finalize()), timeout=15)
        async with sessions() as session:
            assert await session.scalar(select(Customer.balance_rub)) == Decimal("9")
            assert await session.scalar(select(func.count()).select_from(WalletLedger)) == 1
            persisted_event = await session.get(UsageEvent, event_id)
            assert persisted_event is not None and persisted_event.status == "success"

    asyncio.run(run())


def test_failed_assistant_insert_rolls_back_entire_settlement(sessions: Sessions) -> None:
    async def run() -> None:
        payer_id = await add_customer(sessions, "rollback-payer", "50")
        author_id = await add_customer(sessions, "rollback-author", "50")
        async with sessions() as session:
            prompt = Prompt(
                author_customer_id=author_id,
                title="Rollback",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            session.add(prompt)
            await session.commit()
            prompt_id = prompt.id
            event = await billing.start_call(
                session, payer_id, payer_id, "test", "test-model", Decimal("20")
            )
            event_id = event.id
        async with sessions() as session:
            pending_event = await session.get(UsageEvent, event_id)
            saved_prompt = await session.get(Prompt, prompt_id)
            assert pending_event is not None and saved_prompt is not None
            # A real FK failure happens when the combined transaction flushes.
            with pytest.raises(IntegrityError):
                await settle(
                    session,
                    pending_event,
                    prompt=saved_prompt,
                    conversation_id=999999,
                    reply="answer",
                )
            await session.rollback()
        async with sessions() as session:
            balances = list((await session.scalars(select(Customer.balance_rub))).all())
            assert balances == [Decimal("50"), Decimal("50")]
            assert await session.scalar(select(func.count()).select_from(WalletLedger)) == 0
            assert await session.scalar(select(func.count()).select_from(WebMessage)) == 0
            persisted_event = await session.get(UsageEvent, event_id)
            assert persisted_event is not None and persisted_event.status == "pending"
            assert persisted_event.charged_rub is None

    asyncio.run(run())


def test_concurrent_settlement_records_fee_and_assistant_once(sessions: Sessions) -> None:
    async def run() -> None:
        payer_id = await add_customer(sessions, "atomic-payer", "50")
        author_id = await add_customer(sessions, "atomic-author", "50")
        async with sessions() as session:
            prompt = Prompt(
                author_customer_id=author_id,
                title="Atomic",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            conversation = WebConversation(customer_id=payer_id, model_alias="test-model")
            session.add_all([prompt, conversation])
            await session.commit()
            prompt_id, conversation_id = prompt.id, conversation.id
            event = await billing.start_call(
                session, payer_id, payer_id, "test", "test-model", Decimal("20")
            )
            event_id = event.id
        barrier = asyncio.Barrier(2)

        async def finalize() -> None:
            async with sessions() as session:
                event = await session.get(UsageEvent, event_id)
                prompt = await session.get(Prompt, prompt_id)
                assert event is not None and prompt is not None
                await barrier.wait()
                await settle(
                    session, event, prompt=prompt, conversation_id=conversation_id, reply="answer"
                )

        await asyncio.wait_for(asyncio.gather(finalize(), finalize()), timeout=15)
        async with sessions() as session:
            balances = list(
                (await session.scalars(select(Customer.balance_rub).order_by(Customer.id))).all()
            )
            assert balances == [Decimal("39"), Decimal("55")]
            assert await session.scalar(select(func.count()).select_from(WalletLedger)) == 3
            messages = list((await session.scalars(select(WebMessage))).all())
            assert len(messages) == 1
            assert messages[0].content == "answer"
            assert messages[0].usage_event_id == event_id

    asyncio.run(run())


def test_opposing_atomic_settlements_use_consistent_account_lock_order(sessions: Sessions) -> None:
    async def run() -> None:
        first = await add_customer(sessions, "atomic-first", "50")
        second = await add_customer(sessions, "atomic-second", "50")
        async with sessions() as session:
            first_prompt = Prompt(
                author_customer_id=first,
                title="First",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            second_prompt = Prompt(
                author_customer_id=second,
                title="Second",
                system_prompt="test",
                price_rub=Decimal("10"),
            )
            session.add_all([first_prompt, second_prompt])
            await session.commit()
            first_prompt_id, second_prompt_id = first_prompt.id, second_prompt.id
            first_event = await billing.start_call(
                session, first, first, "test", "test-model", Decimal("20")
            )
            second_event = await billing.start_call(
                session, second, second, "test", "test-model", Decimal("20")
            )
            calls = [(first_event.id, second_prompt_id), (second_event.id, first_prompt_id)]
        barrier = asyncio.Barrier(2)

        async def finalize(event_id: UUID, prompt_id: int) -> None:
            async with sessions() as session:
                event = await session.get(UsageEvent, event_id)
                prompt = await session.get(Prompt, prompt_id)
                assert event is not None and prompt is not None
                await barrier.wait()
                await settle(session, event, prompt=prompt)

        await asyncio.wait_for(asyncio.gather(*(finalize(*call) for call in calls)), timeout=15)
        async with sessions() as session:
            assert list((await session.scalars(select(Customer.balance_rub))).all()) == [
                Decimal("44"),
                Decimal("44"),
            ]
            assert await session.scalar(select(func.count()).select_from(WalletLedger)) == 6

    asyncio.run(run())


@pytest.mark.parametrize("other_finalizer", ["failure", "reaper"])
def test_concurrent_finalizers_leave_one_consistent_terminal_result(
    sessions: Sessions, other_finalizer: str
) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "terminal")
        async with sessions() as session:
            event = await billing.start_call(
                session, customer_id, customer_id, "test", "test-model", Decimal("8")
            )
            event_id = event.id
        barrier = asyncio.Barrier(2)

        async def finalize(success: bool) -> None:
            async with sessions() as session:
                event = await session.get(UsageEvent, event_id)
                assert event is not None
                await barrier.wait()
                if success:
                    await settle(session, event)
                elif other_finalizer == "reaper":
                    await billing.reap_pending_call(session, event)
                else:
                    await billing.finalize_failure(
                        session, event, error_code="timeout", latency_ms=1
                    )

        await asyncio.wait_for(asyncio.gather(finalize(True), finalize(False)), timeout=15)
        async with sessions() as session:
            persisted_event = await session.get(UsageEvent, event_id)
            assert persisted_event is not None
            count = await session.scalar(select(func.count()).select_from(WalletLedger))
            balance = await session.scalar(select(Customer.balance_rub))
            if persisted_event.status == "success":
                assert (balance, count, persisted_event.charged_rub) == (
                    Decimal("9"),
                    1,
                    Decimal("1"),
                )
            else:
                assert persisted_event.status == "failed"
                assert (balance, count, persisted_event.charged_rub) == (Decimal("10"), 0, None)

    asyncio.run(run())


def test_purchase_refreshes_the_customer_loaded_by_auth(sessions: Sessions) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "preloaded-purchase")
        async with sessions() as session:
            product = Product(name="Preloaded", price_rub=Decimal("8"))
            session.add(product)
            await session.commit()
            product_id = product.id
        async with sessions() as stale:
            customer = await stale.get(Customer, customer_id)
            saved_product = await stale.get(Product, product_id)
            assert customer is not None and saved_product is not None
            assert customer.balance_rub == 10
            async with sessions() as other:
                current_product = await other.get(Product, product_id)
                assert current_product is not None
                await billing.purchase_subscription(
                    other, customer_id, current_product, "account@integration.invalid", None
                )
            with pytest.raises(billing.InsufficientBalance):
                await billing.purchase_subscription(
                    stale, customer_id, saved_product, "account@integration.invalid", None
                )
        async with sessions() as session:
            assert await session.scalar(select(Customer.balance_rub)) == 2
            assert await session.scalar(select(func.sum(WalletLedger.delta_rub))) == -8

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["credit", "debit", "topup"])
def test_wallet_mutations_refresh_preloaded_customer(sessions: Sessions, operation: str) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "preloaded-mutation")
        async with sessions() as session:
            topup = TopupRequest(customer_id=customer_id, amount_rub=Decimal("3"))
            session.add(topup)
            await session.commit()
            topup_id = topup.id
        async with sessions() as stale:
            customer = await stale.get(Customer, customer_id)
            assert customer is not None and customer.balance_rub == 10
            async with sessions() as other:
                await billing.admin_adjust_balance(
                    other, customer_id, Decimal("5"), "adjustment", "concurrent credit", customer_id
                )
            if operation == "topup":
                saved_topup = await stale.get(TopupRequest, topup_id)
                assert saved_topup is not None
                await billing.confirm_topup(stale, saved_topup, customer_id)
                expected_delta = Decimal("3")
            else:
                expected_delta = Decimal("3") if operation == "credit" else Decimal("-3")
                await billing.admin_adjust_balance(
                    stale, customer_id, expected_delta, "adjustment", "second change", customer_id
                )
        async with sessions() as session:
            assert (
                await session.scalar(select(Customer.balance_rub)) == Decimal("15") + expected_delta
            )
            assert (
                await session.scalar(select(func.sum(WalletLedger.delta_rub))) == 5 + expected_delta
            )

    asyncio.run(run())


@pytest.mark.parametrize("other_operation", ["refund", "fulfill"])
def test_order_can_be_settled_only_once(sessions: Sessions, other_operation: str) -> None:
    async def run() -> None:
        customer_id = await add_customer(sessions, "order-settlement", "20")
        async with sessions() as session:
            product = Product(name="Single settlement", price_rub=Decimal("8"))
            session.add(product)
            await session.commit()
            order = await billing.purchase_subscription(
                session, customer_id, product, "account@integration.invalid", None
            )
            order_id = order.id
        barrier = asyncio.Barrier(2)

        async def settle_order(operation: str) -> None:
            async with sessions() as session:
                pending_order = await session.get(SubscriptionOrder, order_id)
                assert pending_order is not None and pending_order.status == "paid"
                await barrier.wait()
                if operation == "refund":
                    await billing.refund_order(session, pending_order, customer_id)
                else:
                    await billing.fulfill_order(session, pending_order, customer_id)

        await asyncio.wait_for(
            asyncio.gather(settle_order("refund"), settle_order(other_operation)), timeout=15
        )
        async with sessions() as session:
            persisted_order = await session.get(SubscriptionOrder, order_id)
            assert persisted_order is not None
            balance = await session.scalar(select(Customer.balance_rub))
            refund_count = await session.scalar(
                select(func.count())
                .select_from(WalletLedger)
                .where(WalletLedger.entry_type == "refund")
            )
            if persisted_order.status == "refunded":
                assert (balance, refund_count) == (Decimal("20"), 1)
            else:
                assert persisted_order.status == "fulfilled"
                assert (balance, refund_count) == (Decimal("12"), 0)

    asyncio.run(run())
