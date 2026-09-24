"""Owned conversation history and staging of a web chat turn."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EntityNotFound
from app.db.models import Customer, WebConversation, WebMessage, utcnow
from app.services.messages import (
    chat_message_display_text,
    chat_message_provider_content,
    prepare_messages,
)


class DisplayMessage(TypedDict):
    role: str
    text: str
    attachment_name: str | None


@dataclass(frozen=True)
class ConversationPage:
    conversations: Sequence[WebConversation]
    active_conversation: WebConversation | None
    messages: list[DisplayMessage]


@dataclass(frozen=True)
class StagedTurn:
    conversation: WebConversation
    messages: list[dict[str, Any]]
    dlp_found: list[str]


async def owned_conversation(
    session: AsyncSession,
    customer_id: int,
    conversation_id: int,
) -> WebConversation:
    conversation = await session.get(WebConversation, conversation_id)
    if conversation is None or conversation.customer_id != customer_id:
        raise EntityNotFound("Not Found")
    return conversation


async def _history(session: AsyncSession, conversation_id: int) -> Sequence[WebMessage]:
    return (
        await session.scalars(
            select(WebMessage)
            .where(WebMessage.conversation_id == conversation_id)
            .order_by(WebMessage.created_at)
        )
    ).all()


async def conversation_page(
    session: AsyncSession,
    customer_id: int,
    conversation_id: int | None = None,
) -> ConversationPage:
    conversation = (
        await owned_conversation(session, customer_id, conversation_id)
        if conversation_id is not None
        else None
    )
    conversations = (
        await session.scalars(
            select(WebConversation)
            .where(WebConversation.customer_id == customer_id)
            .order_by(WebConversation.updated_at.desc())
        )
    ).all()
    history = await _history(session, conversation.id) if conversation is not None else []
    messages: list[DisplayMessage] = [
        {
            "role": item.role,
            "text": chat_message_display_text(item.content),
            "attachment_name": item.attachment_name,
        }
        for item in history
    ]
    return ConversationPage(conversations, conversation, messages)


async def stage_turn(
    session: AsyncSession,
    customer: Customer,
    model: str,
    conversation: WebConversation | None,
    *,
    message: str,
    stored_content: str,
    content_parts: list[dict[str, Any]] | None,
    attachment_name: str | None,
) -> StagedTurn:
    """Prepare history and stage input; the reservation commits both later."""
    if conversation is None:
        title = (message.strip() or attachment_name or "Новый диалог")[:60]
        conversation = WebConversation(customer_id=customer.id, model_alias=model, title=title)
        session.add(conversation)
        await session.flush()
    conversation.model_alias = model
    conversation.updated_at = utcnow()
    history = await _history(session, conversation.id)
    messages = [
        {"role": item.role, "content": chat_message_provider_content(item.content)}
        for item in history
    ]
    messages.append(
        {"role": "user", "content": content_parts if content_parts is not None else stored_content}
    )
    prepared, dlp_found = prepare_messages(customer, messages, None)
    session.add(
        WebMessage(
            conversation_id=conversation.id,
            role="user",
            content=stored_content,
            attachment_name=attachment_name,
        )
    )
    await session.flush()
    return StagedTurn(conversation, prepared, dlp_found)
