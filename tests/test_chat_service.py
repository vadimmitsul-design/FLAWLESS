"""Behavioral tests for channel differences and cancellation during settlement."""

import asyncio
from decimal import Decimal

import anyio
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.db.models import Customer, UsageEvent, WalletLedger
from app.integrations import llm
from app.services import billing, chat


@pytest.fixture(autouse=True)
def _models():
    llm.init_router()


async def _reserve(session, email):
    customer = Customer(
        email=email,
        name="Chat service test",
        password_hash="unused",
        balance_rub=Decimal("100"),
    )
    session.add(customer)
    await session.flush()
    return await chat.reserve_chat_call(
        session,
        customer,
        "gpt-5-mini",
        [{"role": "user", "content": "hello"}],
        {},
    )


@pytest.mark.parametrize("require_text", [False, True])
def test_tool_call_is_valid_for_api_but_not_for_text_channels(monkeypatch, require_text):
    async def provider(alias, messages, **kwargs):
        return (
            alias,
            "openrouter",
            "openai/gpt-5-mini",
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": '{"city":"Moscow"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    monkeypatch.setattr(llm, "chat_completion_with_fallback", provider)

    async def run():
        async with SessionLocal() as session:
            call = await _reserve(session, f"chatservice_tool_{require_text}@test.local")
            if require_text:
                with pytest.raises(chat.EmptyProviderResponse):
                    await chat.complete_chat_call(session, call, require_text=True)
                assert call.event.status == "failed"
                assert call.event.charged_rub is None
            else:
                result = await chat.complete_chat_call(session, call, save_response_snapshot=True)
                assert result.reply_text is None
                assert result.response["choices"][0]["message"]["tool_calls"]
                assert call.event.status == "success"
                assert call.event.charged_rub > 0
                assert call.event.response_snapshot is not None

    asyncio.run(run())


async def _stream_provider(alias, messages, **kwargs):
    async def chunks():
        yield {"id": "stream_1", "choices": [{"delta": {"content": "A useful partial answer"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return alias, "openrouter", "openai/gpt-5-mini", chunks()


def test_cancelled_stream_scope_finishes_partial_settlement(monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _stream_provider)

    async def run():
        async with SessionLocal() as session:
            call = await _reserve(session, "chatservice_partial_cancel@test.local")
            with anyio.CancelScope() as scope:
                events = chat.stream_chat_call(session, call)
                assert (await anext(events)).kind == "chunk"
                scope.cancel()
                await events.aclose()
            await session.refresh(call.event)
            assert call.event.status == "failed"
            assert call.event.error_code == "ClientDisconnected"
            assert call.event.billing_estimated
            assert call.event.charged_rub > 0

    asyncio.run(run())


def test_cancel_during_success_settlement_does_not_charge_again_as_partial(monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _stream_provider)
    finalize = billing.finalize_success

    async def run():
        async with SessionLocal() as session:
            call = await _reserve(session, "chatservice_success_cancel@test.local")
            with anyio.CancelScope() as scope:

                async def cancel_and_finalize(*args, **kwargs):
                    scope.cancel()
                    await anyio.sleep(0)
                    return await finalize(*args, **kwargs)

                monkeypatch.setattr(billing, "finalize_success", cancel_and_finalize)
                events = [event async for event in chat.stream_chat_call(session, call)]
            assert events[-1].kind == "done"
            event = await session.get(UsageEvent, call.event.id, populate_existing=True)
            assert event.status == "success"
            assert not event.billing_estimated
            count = await session.scalar(
                select(func.count(WalletLedger.id)).where(WalletLedger.usage_event_id == event.id)
            )
            assert count == 1

    asyncio.run(run())
