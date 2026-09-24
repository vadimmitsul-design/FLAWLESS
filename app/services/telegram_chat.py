"""Telegram text-call adapter and preflight for paid voice recognition."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.db.models import Customer
from app.services import billing, dlp
from app.services import chat as chat_service
from app.services.chat import EmptyProviderResponse as EmptyProviderResponse


async def ensure_can_spend(session: AsyncSession, customer: Customer) -> None:
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
    from app.db.models import Customer  # локальный импорт — избежать цикла на уровне модуля

    billing_customer_id = billing.resolve_billing_customer_id(customer)
    await billing.check_spend_limits(session, billing_customer_id)
    payer = await session.get(Customer, billing_customer_id)
    if payer is None:
        raise billing.InsufficientBalance(Decimal(0))
    available = await billing.available_balance(session, billing_customer_id, payer.balance_rub)
    if available <= 0:
        raise billing.InsufficientBalance(available)


class TooManyRequests(Exception):
    """Слишком частые обращения от одного человека."""


async def run_chat_turn(
    session: AsyncSession, customer_id: int, model_alias: str, messages: list[dict]
) -> str:
    """Списывает с баланса ПЛАТЕЛЬЩИКА (см. billing.resolve_billing_customer_id
    для детских аккаунтов), возвращает текст ответа ассистента. Бросает
    billing.InsufficientBalance/SpendLimitExceeded/KeyError (неизвестная
    модель) — вызывающий код сам решает, как это показать пользователю."""
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise billing.InsufficientBalance(Decimal(0))
    if not ratelimit.check(ratelimit.customer_bucket(customer_id)):
        raise TooManyRequests()
    await billing.check_spend_limits(session, billing.resolve_billing_customer_id(customer))
    messages, dlp_found = dlp.redact_messages(messages)
    call = await chat_service.reserve_chat_call(
        session,
        customer,
        model_alias,
        messages,
        {},
        dlp_found=dlp_found,
    )
    try:
        result = await chat_service.complete_chat_call(session, call, require_text=True)
    except chat_service.ProviderCallFailed as exc:
        raise exc.cause from exc
    assert result.reply_text is not None
    return result.reply_text
