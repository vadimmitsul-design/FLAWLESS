"""Общая логика одного НЕстримингового обращения к модели с биллингом —
переиспользуется /v1/chat/completions (напрямую вшито в main.py, там ещё
есть prompt_id/детский режим/стриминг) и Telegram-секретарём (который всего
этого не поддерживает — просто задал вопрос и получил ответ)."""

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app import billing, dlp, llm, pricing, ratelimit


class TooManyRequests(Exception):
    """Слишком частые обращения от одного человека."""


async def run_chat_turn(session: AsyncSession, customer_id: int, model_alias: str, messages: list[dict]) -> str:
    """Списывает с баланса ПЛАТЕЛЬЩИКА (см. billing.resolve_billing_customer_id
    для детских аккаунтов), возвращает текст ответа ассистента. Бросает
    billing.InsufficientBalance/SpendLimitExceeded/KeyError (неизвестная
    модель) — вызывающий код сам решает, как это показать пользователю."""
    from app.models import Customer, utcnow  # локальный импорт — избежать цикла на уровне модуля

    customer = await session.get(Customer, customer_id)
    billing_customer_id = billing.resolve_billing_customer_id(customer)

    # Те же ограничители, что у остальных дверей: раньше Telegram не проверял
    # ни частоту, ни потолок расхода вообще.
    if not ratelimit.check(ratelimit.customer_bucket(customer_id)):
        raise TooManyRequests()
    await billing.check_spend_limits(session, billing_customer_id)

    provider, model = llm.resolve_alias(model_alias)

    redacted_messages, dlp_found = dlp.redact_messages(messages)

    pricing_cfg = await billing.get_pricing_config(session)
    # Оценка ДО вызова — по ИСХОДНО запрошенной модели, для резерва (1.1).
    # Реальный провайдер/модель, что фактически ответит, известен только
    # после fallback (llm.chat_completion_with_fallback) — цену для
    # ФАКТИЧЕСКОГО списания резолвим заново ниже, не переиспользуем эту.
    estimate_price = await pricing.find_price(session, provider, model, None, None, utcnow())
    reserve_rub = billing.estimate_reserve_rub(estimate_price, redacted_messages, {}, pricing_cfg)

    event = await billing.start_call(
        session, customer_id, billing_customer_id, provider, model, estimated_reserve_rub=reserve_rub
    )
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
