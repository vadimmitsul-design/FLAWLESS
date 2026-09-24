"""api / v1 / chat for the Flawless application."""

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_customer_by_api_key
from app.core import ratelimit
from app.core.config import (
    settings,
)
from app.core.errors import EntityNotFound
from app.db import get_session
from app.db.models import (
    ApiKey,
    Customer,
    UsageEvent,
)
from app.integrations import llm
from app.schemas import ChatCompletionRequest
from app.services import billing, pricing, prompts
from app.services import chat as chat_service
from app.services.messages import ChildRequestBlocked, prepare_messages

logger = logging.getLogger(__name__)


router = APIRouter()


# ---------- API: OpenAI-совместимый чат ----------

# Схема запроса — extra="allow" (см. app/schemas.py), потому что параметры
# вроде temperature/max_tokens прозрачно летят в LiteLLM. Но LiteLLM также
# принимает api_base/api_key/base_url и т.п. как per-call override —
# пропусти их клиенту, и он подменит эндпоинт вызова своим доменом: сервер
# отправит туда наш настоящий ключ провайдера (SSRF + утечка ключа). Поэтому
# белый список, а не чёрный — новый опасный параметр в LiteLLM не появится
# здесь сам по себе. mock_response — официальный тестовый параметр LiteLLM
# (используется в tests/), безопасен: не делает исходящих вызовов.
_ALLOWED_EXTRA_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "stream",
    "n",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "user",
    "logprobs",
    "top_logprobs",
}


# stream_options СОЗНАТЕЛЬНО убран из списка: единственное, что там есть, —
# include_usage, а выключенный include_usage означает, что провайдер не
# пришлёт финальный usage-чанк. Без usage цена не считается, charged_rub
# остаётся NULL, записи в журнал нет — вызов проходит БЕСПЛАТНО при
# полностью реальном расходе у поставщика. Клиенту здесь нечего настраивать:
# сервер ставит include_usage сам и безусловно.
#
# mock_response — тестовый параметр LiteLLM: провайдер не вызывается вовсе,
# ответ выдумывается на месте. В тестах он нужен, в проде это способ получить
# «ответ» и заплатить за него настоящими деньгами при нулевой себестоимости.
# Проверяется на КАЖДОМ запросе, а не один раз при импорте: иначе смена
# ENVIRONMENT требовала бы пересборки образа, а проверить это тестом было бы
# нечем.
_DEV_ONLY_EXTRA_PARAMS = {"mock_response"}


def _allowed_extra_params() -> set[str]:
    if settings.environment == "production":
        return _ALLOWED_EXTRA_PARAMS
    return _ALLOWED_EXTRA_PARAMS | _DEV_ONLY_EXTRA_PARAMS


def _idempotency_request_hash(model: str, messages: list, prompt_id: int | None) -> str:
    """Повтор с тем же Idempotency-Key, но ДРУГИМ телом запроса — не должен
    молча вернуть чужой кэшированный ответ (находка состязательного ревью
    2026-09-04)."""
    payload = json.dumps(
        {"model": model, "messages": messages, "prompt_id": prompt_id}, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay_idempotent_response(existing: UsageEvent):
    if existing.status == "pending":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": "request with this Idempotency-Key is still processing",
                    "type": "idempotency_conflict",
                }
            },
        )
    if existing.response_snapshot is not None:
        return JSONResponse(
            status_code=existing.response_status_code or 200,
            content=json.loads(existing.response_snapshot),
        )
    # Стриминговые ответы снапшот не сохраняют (см. _stream_chat_completion) —
    # честно сообщаем, что повтор для них не воспроизводится байт-в-байт,
    # а не тихо отдаём пустой/неверный ответ.
    raise HTTPException(
        status_code=409,
        detail={
            "error": {
                "message": "request with this Idempotency-Key was already processed "
                "(streamed responses cannot be replayed)",
                "type": "idempotency_replay_unavailable",
            }
        },
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    auth: tuple[Customer, ApiKey] = Depends(get_customer_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    customer, api_key = auth

    if not ratelimit.check(ratelimit.api_key_bucket(api_key.id)):
        raise HTTPException(
            status_code=429,
            detail={
                "error": {"message": "rate limit exceeded, slow down", "type": "rate_limit_error"}
            },
        )

    # Потолки расхода — до резерва и вызова провайдера, чтобы не тратить
    # деньги на заведомо заблокированный запрос. Проверяются оба уровня:
    # на кошельке (не обходится вторым ключом) и на самом ключе.
    try:
        await billing.check_spend_limits(
            session, billing.resolve_billing_customer_id(customer), api_key
        )
    except billing.SpendLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"{e.period} spend limit exceeded for this {e.scope}",
                    "type": "spend_limit_exceeded",
                    "scope": e.scope,
                    "limit_rub": str(e.limit),
                    "spent_rub": str(e.spent),
                }
            },
        ) from None

    allowed_params = _allowed_extra_params()
    extra = {
        k: v
        for k, v in body.model_dump(exclude={"model", "messages", "prompt_id"}).items()
        if k in allowed_params
    }
    # Предел длины ответа зажимаем ДО расчёта резерва: обе величины читают
    # один и тот же extra, поэтому оценка и факт сходятся по построению.
    extra = pricing.clamp_output_tokens(extra)

    try:
        llm.resolve_alias(body.model)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "message": f"unknown model '{body.model}'",
                    "type": "invalid_request_error",
                }
            },
        ) from None

    prompt = None
    if body.prompt_id is not None:
        # Раздел промптов выключен — значит и через API их не подставить,
        # иначе выключение раздела закрывало бы только интерфейс.
        try:
            prompt = await prompts.active_prompt(
                session, body.prompt_id, enabled=settings.enable_prompts
            )
        except EntityNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": {"message": exc.message, "type": "invalid_request_error"}},
            ) from exc

    try:
        messages, dlp_found = prepare_messages(customer, body.messages, prompt)
    except ChildRequestBlocked:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "Недоступно в детском режиме — попроси объяснить тему, а не готовое сочинение/реферат.",
                    "type": "child_mode_blocked",
                }
            },
        ) from None

    # Идемпотентность (1.4 доработок): Idempotency-Key — обычный HTTP-заголовок
    # (как у Stripe), не поле тела. По actor'у (customer.id), НЕ по
    # billing_customer_id — иначе два ребёнка одного родителя делили бы одно
    # пространство ключей (находка состязательного ревью 2026-09-04).
    # Проверяем ДО start_call — повтор не должен ни списывать деньги повторно,
    # ни дублировать вызов провайдера. Хэш тела запроса — чтобы повтор с тем
    # же ключом, но ДРУГИМ запросом, не вернул молча чужой кэшированный ответ.
    idempotency_key = (request.headers.get("idempotency-key") or "").strip()[:200] or None
    request_hash = None
    if idempotency_key is not None:
        request_hash = _idempotency_request_hash(body.model, body.messages, body.prompt_id)
        existing = await billing.find_event_by_idempotency_key(
            session, customer.id, idempotency_key
        )
        if existing is not None:
            if (
                existing.idempotency_request_hash is not None
                and existing.idempotency_request_hash != request_hash
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": {
                            "message": "this Idempotency-Key was already used with a different request",
                            "type": "idempotency_key_reused",
                        }
                    },
                )
            return _replay_idempotent_response(existing)

    try:
        call = await chat_service.reserve_chat_call(
            session,
            customer,
            body.model,
            messages,
            extra,
            api_key=api_key,
            prompt=prompt,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            dlp_found=dlp_found,
        )
    except billing.ModelNotPriced:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": f"model '{body.model}' is temporarily unavailable: no active price configured",
                    "type": "model_not_priced",
                }
            },
        ) from None
    except billing.InsufficientBalance as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": "insufficient balance, top up at /",
                    "type": "insufficient_quota",
                    "balance_rub": str(exc.balance),
                }
            },
        ) from None
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": "request with this Idempotency-Key conflicted, please retry",
                    "type": "idempotency_conflict",
                }
            },
        ) from None

    if extra.get("stream"):
        return StreamingResponse(
            _stream_chat_completion(session, call), media_type="text/event-stream"
        )
    try:
        result = await chat_service.complete_chat_call(session, call, save_response_snapshot=True)
    except chat_service.ProviderCallFailed as exc:
        raise HTTPException(
            status_code={"rate_limit": 503, "timeout": 504, "provider": 502}[exc.kind],
            detail={"error": {"message": str(exc), "type": "provider_error"}},
        ) from exc
    return result.response


async def _stream_chat_completion(
    session: AsyncSession,
    call: chat_service.ChatCall,
) -> AsyncIterator[str]:
    """Translate service events to SSE and propagate client disconnects."""
    async with aclosing(chat_service.stream_chat_call(session, call)) as events:
        async for event in events:
            if event.kind == "done":
                yield "data: [DONE]\n\n"
            else:
                yield f"data: {json.dumps(event.data)}\n\n"
