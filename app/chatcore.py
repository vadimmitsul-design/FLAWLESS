"""Общая логика одного НЕстримингового обращения к модели с биллингом —
переиспользуется /v1/chat/completions (напрямую вшито в main.py, там ещё
есть prompt_id/детский режим/стриминг) и Telegram-секретарём (который всего
этого не поддерживает — просто задал вопрос и получил ответ)."""

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app import billing, dlp, llm, pricing


async def run_chat_turn(session: AsyncSession, customer_id: int, model_alias: str, messages: list[dict]) -> str:
    """Списывает с баланса ПЛАТЕЛЬЩИКА (см. billing.resolve_billing_customer_id
    для детских аккаунтов), возвращает текст ответа ассистента. Бросает
    billing.InsufficientBalance/KeyError (неизвестная модель) — вызывающий
    код сам решает, как это показать пользователю."""
    from app.models import Customer  # локальный импорт — избежать цикла на уровне модуля

    customer = await session.get(Customer, customer_id)
    billing_customer_id = billing.resolve_billing_customer_id(customer)
    provider, model = llm.resolve_alias(model_alias)

    redacted_messages, dlp_found = dlp.redact_messages(messages)

    event = await billing.start_call(session, customer_id, billing_customer_id, provider, model)
    if dlp_found:
        event.dlp_redactions = ",".join(sorted(set(dlp_found)))

    started = time.monotonic()
    try:
        used_alias, provider, model, response = await llm.chat_completion_with_fallback(
            model_alias, redacted_messages
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        raise

    event.provider = provider
    event.model = model
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = llm.extract_chat_usage(response)
    price = await pricing.find_price(session, provider, model, None, None, event.created_at)
    cost_usd = pricing.compute_cost(price, usage)
    pricing_cfg = await billing.get_pricing_config(session)
    await billing.finalize_success(
        session,
        event,
        usage=usage,
        cost_usd=cost_usd,
        price_id=price.id if price else None,
        litellm_cost=llm.extract_litellm_cost(response),
        pricing_cfg=pricing_cfg,
        latency_ms=latency_ms,
        provider_request_id=llm.extract_call_id(response),
    )
    return llm.to_dict(response)["choices"][0]["message"]["content"]
