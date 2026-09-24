"""api / chat for the Flawless application."""

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_customer
from app.core import ratelimit
from app.db import get_session
from app.db.models import Customer
from app.integrations import llm
from app.services import billing, conversations
from app.services import chat as chat_service
from app.services.catalog import available_models
from app.services.messages import (
    ChildRequestBlocked,
)

logger = logging.getLogger(__name__)


router = APIRouter()


# ---------- веб: чат в кабинете (2.3 доработок) ----------
# Третья дверь входа рядом с API-ключом и Telegram — для клиентов, которые
# никогда не видели API-ключа. Тот же путь биллинга, что и /v1/chat/completions
# (billing.estimate_reserve_rub/start_call/finalize_*) — никакой отдельной
# логики списания, только другая обвязка вокруг тех же функций.

_CHAT_ALLOWED_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log"}


_CHAT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # с запасом под фото; не под видео/архивы


@router.get("/chat")
async def web_chat(
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    page = await conversations.conversation_page(session, customer.id)
    return request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {
            "customer": customer,
            "conversations": page.conversations,
            "active_conversation": None,
            "chat_messages": [],
            "models": await available_models(session),
        },
    )


@router.get("/chat/{conversation_id}")
async def web_chat_conversation(
    conversation_id: int,
    request: Request,
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if customer is None:
        return RedirectResponse("/login", status_code=303)
    page = await conversations.conversation_page(session, customer.id, conversation_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {
            "customer": customer,
            "conversations": page.conversations,
            "active_conversation": page.active_conversation,
            "chat_messages": page.messages,
            "models": await available_models(session),
        },
    )


@router.post("/chat/send")
async def web_chat_send(
    conversation_id: int | None = Form(None),
    model: str = Form(...),
    message: str = Form(""),
    file: UploadFile | None = File(None),
    customer: Customer | None = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if customer is None:
        raise HTTPException(status_code=401)

    # Те же ограничители, что и у API-двери: раньше веб-чат не проверял
    # ни частоту, ни потолок расхода — лимит обходился переходом сюда.
    # Частота считается по человеку (ключа здесь нет), потолок — по кошельку.
    if not ratelimit.check(ratelimit.customer_bucket(customer.id)):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите немного")
    try:
        await billing.check_spend_limits(session, billing.resolve_billing_customer_id(customer))
    except billing.SpendLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=f"Достигнут лимит расхода ({e.period}): потрачено {e.spent} ₽ из {e.limit} ₽",
        ) from None

    try:
        llm.resolve_alias(model)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown model '{model}'") from None

    conversation = None
    if conversation_id is not None:
        conversation = await conversations.owned_conversation(session, customer.id, conversation_id)

    if not message.strip() and file is None:
        raise HTTPException(status_code=400, detail="empty message")

    attachment_name = None
    content_parts: list[dict[str, Any]] | None = None
    extra_text = ""
    if file is not None and file.filename:
        # Размер известен парсеру до чтения — отказываем, не материализуя
        # файл в памяти. Чтение всё равно чанками: на запрос без
        # Content-Length (chunked) размер заранее неизвестен.
        if (file.size or 0) > _CHAT_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="file too large (5MB max)")
        chunks: list[bytes] = []
        read = 0
        while chunk := await file.read(64 * 1024):
            read += len(chunk)
            if read > _CHAT_MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=400, detail="file too large (5MB max)")
            chunks.append(chunk)
        data = b"".join(chunks)
        attachment_name = file.filename
        content_type = file.content_type or ""
        ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode("ascii")
            content_parts = [
                {"type": "text", "text": message.strip() or "Что на этом изображении?"},
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            ]
        elif ext in _CHAT_ALLOWED_TEXT_EXTENSIONS:
            try:
                extra_text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="text file must be UTF-8") from None
        else:
            raise HTTPException(
                status_code=400,
                detail="unsupported file type — only images and text files (.txt/.md/.csv/.json/.log)",
            )

    if content_parts is not None:
        stored_content = json.dumps(content_parts)
    else:
        user_text = message.strip()
        if extra_text:
            user_text = (
                f"{user_text}\n\nПрикреплённый файл {attachment_name}:\n{extra_text}".strip()
            )
        stored_content = user_text

    try:
        turn = await conversations.stage_turn(
            session,
            customer,
            model,
            conversation,
            message=message,
            stored_content=stored_content,
            content_parts=content_parts,
            attachment_name=attachment_name,
        )
    except ChildRequestBlocked:
        raise HTTPException(
            status_code=400,
            detail="Недоступно в детском режиме — попроси объяснить тему, а не готовое сочинение/реферат.",
        ) from None

    try:
        call = await chat_service.reserve_chat_call(
            session,
            customer,
            model,
            turn.messages,
            {},
            dlp_found=turn.dlp_found,
        )
    except billing.ModelNotPriced:
        raise HTTPException(
            status_code=503,
            detail=f"Модель «{model}» сейчас недоступна: не настроена цена. Сообщите администратору.",
        ) from None
    except billing.InsufficientBalance as exc:
        raise HTTPException(
            status_code=402,
            detail=f"insufficient balance — нужно ~{exc.required} ₽, доступно {exc.balance} ₽",
        ) from None
    try:
        result = await chat_service.complete_chat_call(
            session,
            call,
            require_text=True,
            web_conversation_id=turn.conversation.id,
        )
    except chat_service.ProviderCallFailed as exc:
        logger.warning("web chat call failed for event %s: %r", call.event.id, exc.cause)
        raise HTTPException(
            status_code=502, detail="провайдер сейчас недоступен, попробуйте ещё раз"
        ) from exc
    except chat_service.EmptyProviderResponse as exc:
        raise HTTPException(
            status_code=502,
            detail="модель вернула пустой ответ — попробуйте переформулировать запрос",
        ) from exc
    return JSONResponse(
        {
            "conversation_id": turn.conversation.id,
            "conversation_title": turn.conversation.title,
            "reply": result.reply_text,
        }
    )
