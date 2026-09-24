"""The paid model-call lifecycle shared by API, web chat and Telegram.

Adapters validate their input and prepare messages. This module owns reservation,
provider usage and finalization; transport formatting stays with the adapters.
"""

import json
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import anyio
import litellm
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, Customer, PricingConfig, Prompt, UsageEvent, utcnow
from app.integrations import llm
from app.services import billing, pricing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatCall:
    event: UsageEvent
    alias: str
    messages: list[dict[str, Any]]
    extra: dict[str, Any]
    pricing_config: PricingConfig
    allowed_aliases: set[str]
    prompt: Prompt | None = None


@dataclass(frozen=True)
class ChatResult:
    response: dict[str, Any]
    reply_text: str | None


@dataclass(frozen=True)
class ChatStreamEvent:
    kind: Literal["chunk", "error", "done"]
    data: dict[str, Any] | None = None


class EmptyProviderResponse(Exception):
    """A text-only channel received no usable assistant text."""


class ProviderCallFailed(Exception):
    """A provider call failed after its reservation was released."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        self.kind: Literal["rate_limit", "timeout", "provider"] = (
            "rate_limit"
            if isinstance(cause, litellm.RateLimitError)
            else "timeout"
            if isinstance(cause, litellm.Timeout)
            else "provider"
        )
        super().__init__(str(cause))


def reply_text_or_none(response: dict[str, Any]) -> str | None:
    """Text validation is optional: API tool calls legitimately omit content."""
    choices = response.get("choices") or []
    if not choices:
        return None
    content = ((choices[0] or {}).get("message") or {}).get("content")
    return content if isinstance(content, str) and content.strip() else None


async def reserve_chat_call(
    session: AsyncSession,
    customer: Customer,
    alias: str,
    messages: list[dict[str, Any]],
    extra: dict[str, Any],
    *,
    api_key: ApiKey | None = None,
    prompt: Prompt | None = None,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
    dlp_found: Sequence[str] = (),
) -> ChatCall:
    """Persist the reservation and its metadata before contacting a provider."""
    provider, model = llm.resolve_alias(alias)
    extra = pricing.clamp_output_tokens(extra)
    config = await billing.get_pricing_config(session)
    now = utcnow()
    await billing.price_for_call(session, provider, model, now)
    reserve = await billing.estimate_reserve_for_chain(
        session,
        alias,
        messages,
        extra,
        config,
        now,
        extra_fixed_rub=prompt.price_rub if prompt is not None else Decimal(0),
    )
    # Read provider policy before committing the reservation: no database
    # transaction or row lock needs to span the external model call.
    allowed_aliases = await llm.priced_aliases(session, now)
    event = await billing.start_call(
        session,
        customer.id,
        billing.resolve_billing_customer_id(customer),
        provider,
        model,
        estimated_reserve_rub=reserve,
        idempotency_key=idempotency_key,
        idempotency_request_hash=request_hash,
        api_key_id=api_key.id if api_key is not None else None,
        prompt_id=prompt.id if prompt is not None else None,
        dlp_redactions=",".join(sorted(set(dlp_found))) if dlp_found else None,
    )
    return ChatCall(event, alias, messages, extra, config, allowed_aliases, prompt)


async def complete_chat_call(
    session: AsyncSession,
    call: ChatCall,
    *,
    require_text: bool = False,
    save_response_snapshot: bool = False,
    web_conversation_id: int | None = None,
) -> ChatResult:
    """Complete a nonstreaming call and atomically persist its money and reply."""
    event = call.event
    started = time.monotonic()
    try:
        used_alias, provider, model, response = await llm.chat_completion_with_fallback(
            call.alias,
            call.messages,
            allowed_aliases=call.allowed_aliases,
            **call.extra,
        )
    except Exception as exc:
        failure = ProviderCallFailed(exc)
        snapshot = None
        status_code = None
        if save_response_snapshot:
            # The API replay record preserves its existing wire contract.
            snapshot = json.dumps({"error": {"message": str(exc), "type": "provider_error"}})
            status_code = {"rate_limit": 503, "timeout": 504, "provider": 502}[failure.kind]
        await billing.finalize_failure(
            session,
            event,
            error_code=type(exc).__name__,
            latency_ms=int((time.monotonic() - started) * 1000),
            response_snapshot=snapshot,
            response_status_code=status_code,
        )
        raise failure from exc

    if used_alias != call.alias:
        logger.info(
            "event %s served by fallback %s instead of %s", event.id, used_alias, call.alias
        )
    event.provider, event.model = provider, model
    latency_ms = int((time.monotonic() - started) * 1000)
    response_dict = llm.to_dict(response)
    reply = reply_text_or_none(response_dict)
    if require_text and reply is None:
        await billing.finalize_failure(
            session,
            event,
            error_code="EmptyProviderResponse",
            latency_ms=latency_ms,
        )
        raise EmptyProviderResponse()

    usage = llm.extract_chat_usage(response)
    price = await pricing.find_price(session, provider, model, None, None, event.created_at)
    cost = pricing.compute_cost(price, usage)
    if cost is None:
        logger.warning("no cost computed for event %s (%s/%s)", event.id, provider, model)
    with anyio.CancelScope(shield=True):
        await billing.finalize_success(
            session,
            event,
            usage=usage,
            cost_usd=cost,
            price_id=price.id if price else None,
            litellm_cost=llm.extract_litellm_cost(response),
            pricing_cfg=call.pricing_config,
            latency_ms=latency_ms,
            provider_request_id=llm.extract_call_id(response),
            response_snapshot=json.dumps(response_dict) if save_response_snapshot else None,
            response_status_code=200 if save_response_snapshot else None,
            prompt=call.prompt,
            web_conversation_id=web_conversation_id,
            reply_text=reply,
        )
    return ChatResult(response_dict, reply)


async def stream_chat_call(
    session: AsyncSession, call: ChatCall
) -> AsyncGenerator[ChatStreamEvent, None]:
    """Yield transport-neutral chunks while owning success and partial billing."""
    event = call.event
    extra = {**call.extra, "stream": True, "stream_options": {"include_usage": True}}
    started = time.monotonic()
    usage = pricing.UsageAmounts()
    call_id = None
    content = ""
    provider: str | None = None
    model: str | None = None
    finalized = False

    async def close_partial(error_code: str) -> None:
        nonlocal finalized
        if finalized:
            return
        partial_usage = None
        cost = None
        price_id = None
        if content and provider is not None and model is not None:
            price = await pricing.find_price(session, provider, model, None, None, event.created_at)
            partial_usage = pricing.UsageAmounts(
                input_text_tokens=pricing.estimate_messages_tokens(call.messages),
                output_tokens=pricing.estimate_tokens_from_text(content),
            )
            cost = pricing.compute_cost(price, partial_usage)
            price_id = price.id if price else None
        await billing.finalize_failure(
            session,
            event,
            error_code=error_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=partial_usage,
            cost_usd=cost,
            price_id=price_id,
            pricing_cfg=call.pricing_config,
            estimated=partial_usage is not None,
        )
        finalized = True

    try:
        try:
            _, provider, model, stream = await llm.chat_completion_with_fallback(
                call.alias,
                call.messages,
                allowed_aliases=call.allowed_aliases,
                **extra,
            )
            event.provider, event.model = provider, model
            async for chunk in stream:
                data = llm.to_dict(chunk)
                call_id = call_id or data.get("id")
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    delta_content = delta.get("content")
                    if isinstance(delta_content, str):
                        content += delta_content
                    for tool_call in delta.get("tool_calls") or []:
                        fragment = ((tool_call or {}).get("function") or {}).get("arguments")
                        if isinstance(fragment, str):
                            content += fragment
                if data.get("usage"):
                    usage = llm.extract_chat_usage(data)
                yield ChatStreamEvent("chunk", data)
        except Exception as exc:
            logger.warning("provider stream failed for event %s: %r", event.id, exc)
            await close_partial(type(exc).__name__)
            yield ChatStreamEvent(
                "error", {"error": {"message": str(exc), "type": "provider_error"}}
            )
            yield ChatStreamEvent("done")
            return

        price = await pricing.find_price(session, provider, model, None, None, event.created_at)
        cost = pricing.compute_cost(price, usage)
        estimated = False
        if cost is None and price is not None and content:
            usage = pricing.UsageAmounts(
                input_text_tokens=pricing.estimate_messages_tokens(call.messages),
                output_tokens=pricing.estimate_tokens_from_text(content),
            )
            cost = pricing.compute_cost(price, usage)
            estimated = cost is not None
        with anyio.CancelScope(shield=True):
            await billing.finalize_success(
                session,
                event,
                usage=usage,
                cost_usd=cost,
                price_id=price.id if price else None,
                litellm_cost=None,
                pricing_cfg=call.pricing_config,
                latency_ms=int((time.monotonic() - started) * 1000),
                provider_request_id=call_id,
                billing_estimated=estimated,
                prompt=call.prompt,
            )
        finalized = True
        yield ChatStreamEvent("done")
    finally:
        if not finalized:
            # Starlette cancels an AnyIO scope when the client disconnects.
            # Shield the database cleanup until its transaction has finished.
            with anyio.CancelScope(shield=True):
                await close_partial("ClientDisconnected")
