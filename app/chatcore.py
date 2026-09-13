"""Общая логика одного НЕстримингового обращения к модели с биллингом —
переиспользуется /v1/chat/completions (напрямую вшито в main.py, там ещё
есть prompt_id/детский режим/стриминг) и Telegram-секретарём (который всего
этого не поддерживает — просто задал вопрос и получил ответ)."""

import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app import billing, dlp, llm, pricing, ratelimit


async def ensure_can_spend(session: AsyncSession, customer) -> None:
    """Те же проверки, что делает run_chat_turn, но ДО платного действия.

    Нужна там, где деньги тратятся раньше самого вызова модели: распознавание
    голосового — платный вызов Whisper, и раньше он шёл вообще без проверок.
    Бросает те же исключения, что и run_chat_turn, чтобы вызывающий показывал
    человеку один и тот же текст.
    """
    # Тот же ключ ведёрка, что и у run_chat_turn: на голых строках ключи
    # уже расходились однажды («key:5» против «customer:5»). Голосовое
    # сообщение съедает ДВА слота — распознавание и сам вызов модели — и это
    # верно: это два платных обращения, а не одно.
    if not ratelimit.check(ratelimit.customer_bucket(customer.id)):
        raise TooManyRequests()
    from app.models import Customer  # локальный импорт — избежать цикла на уровне модуля

    billing_customer_id = billing.resolve_billing_customer_id(customer)
    await billing.check_spend_limits(session, billing_customer_id)
    payer = await session.get(Customer, billing_customer_id)
    if payer is None:
        raise billing.InsufficientBalance(Decimal(0))
    available = await billing.available_balance(session, billing_customer_id, payer.balance_rub)
    if available <= 0:
        raise billing.InsufficientBalance(available)


class EmptyProviderResponse(Exception):
    """Модель ответила, но текста в ответе нет.

    `content: null` — штатный отказ модели (safety-блок), `choices: []` —
    сбой у поставщика. Разбор по индексам ронял обработчик ПОСЛЕ списания:
    деньги ушли, а клиенту уходило буквально слово «None». Считаем это
    неудавшимся вызовом: событие закрывается ошибкой, резерв снимается,
    денег не берём.
    """


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
    # Нет действующей цены — отказываемся до вызова провайдера (иначе расход
    # у него идёт, а с клиента не списывается ничего). Бросается наружу,
    # telegram_bot показывает это человеку понятным текстом.
    estimate_price = await billing.price_for_call(session, provider, model, utcnow())
    call_extra = pricing.clamp_output_tokens({})
    reserve_rub = await billing.estimate_reserve_for_chain(
        session, model_alias, redacted_messages, call_extra, pricing_cfg, utcnow()
    )

    event = await billing.start_call(
        session, customer_id, billing_customer_id, provider, model, estimated_reserve_rub=reserve_rub
    )
    if dlp_found:
        event.dlp_redactions = ",".join(sorted(set(dlp_found)))

    started = time.monotonic()
    try:
        used_alias, provider, model, response = await llm.chat_completion_with_fallback(
            model_alias,
            redacted_messages,
            allowed_aliases=await llm.priced_aliases(session, utcnow()),
            **call_extra,
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        await billing.finalize_failure(session, event, error_code=type(e).__name__, latency_ms=latency_ms)
        raise

    event.provider = provider
    event.model = model
    latency_ms = int((time.monotonic() - started) * 1000)
    choices = (llm.to_dict(response) or {}).get("choices") or []
    message = (choices[0] or {}).get("message") or {} if choices else {}
    reply = message.get("content")
    if not (isinstance(reply, str) and reply.strip()):
        # Проверяем ДО списания: платить за ответ, которого нет, незачем.
        await billing.finalize_failure(
            session, event, error_code="EmptyProviderResponse", latency_ms=latency_ms
        )
        raise EmptyProviderResponse()

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
    return reply
