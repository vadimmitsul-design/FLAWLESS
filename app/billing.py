"""Биллинг кошелька в рублях.

В отличие от AI-HUB/gateway (фиксированная цена операции списывается ДО
вызова), здесь клиент сам выбирает модель — точная стоимость известна только
ПОСЛЕ ответа провайдера (зависит от длины completion). Поэтому: перед
вызовом только проверяем balance_rub > 0 (не списываем), после вызова
списываем по факту (себестоимость * наценка * курс). Баланс может ненадолго
уйти в минус на последнем вызове — следующий вызов уже блокируется этой же
проверкой. Ledger append-only, как везде в этом кодовом кусте.
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Customer,
    PricingConfig,
    Product,
    Prompt,
    SubscriptionOrder,
    TopupRequest,
    UsageEvent,
    WalletLedger,
    utcnow,
)
from app.pricing import UsageAmounts

_RUB_QUANT = Decimal("0.0001")


class InsufficientBalance(Exception):
    def __init__(self, balance: Decimal, required: Decimal | None = None):
        self.balance = balance
        self.required = required
        super().__init__(f"balance {balance} < required {required}" if required else f"balance {balance} <= 0")


async def get_pricing_config(session: AsyncSession) -> PricingConfig:
    cfg = await session.get(PricingConfig, 1)
    if cfg is None:
        raise RuntimeError("pricing_config не засеян — запустить scripts/seed_prices.py")
    return cfg


def resolve_billing_customer_id(customer: Customer) -> int:
    """Детский аккаунт (is_child) тратит с баланса родителя — так и
    задумано (родитель оплачивает и видит всю историю ребёнка)."""
    return customer.parent_customer_id if customer.is_child and customer.parent_customer_id else customer.id


async def start_call(
    session: AsyncSession, actor_customer_id: int, billing_customer_id: int, provider: str, model: str
) -> UsageEvent:
    """Транзакция 1: проверить баланс ПЛАТЕЛЬЩИКА > 0, создать pending-событие
    с actor_customer_id (кто реально вызвал) отдельно от billing_customer_id
    (с чьего баланса спишется — для детских аккаунтов это родитель).
    Коммитит сама."""
    payer = await session.get(Customer, billing_customer_id, with_for_update=True)
    if payer.balance_rub <= 0:
        # Намеренно НЕ делаем rollback здесь: он экспайрит вообще все объекты
        # в сессии (включая customer из Depends(get_current_customer) и любые
        # другие уже загруженные в этом запросе) — следующее же обращение к
        # их атрибутам роняет MissingGreenlet. FOR UPDATE-лок снимется сам,
        # когда FastAPI закроет сессию в конце запроса — это безопаснее.
        raise InsufficientBalance(payer.balance_rub)

    event = UsageEvent(
        customer_id=actor_customer_id,
        billing_customer_id=billing_customer_id,
        provider=provider,
        model=model,
        status="pending",
    )
    session.add(event)
    await session.commit()
    return event


def price_in_rub(cost_usd: Decimal, cfg: PricingConfig) -> Decimal:
    return (cost_usd * (1 + cfg.markup_percent / 100) * cfg.usd_rub_rate).quantize(_RUB_QUANT)


async def finalize_success(
    session: AsyncSession,
    event: UsageEvent,
    *,
    usage: UsageAmounts,
    cost_usd: Decimal | None,
    price_id: int | None,
    litellm_cost: float | None,
    pricing_cfg: PricingConfig,
    latency_ms: int,
    provider_request_id: str | None,
) -> Decimal:
    """Транзакция 2 (успех): списать по факту, вернуть новый баланс."""
    charged_rub = price_in_rub(cost_usd, pricing_cfg) if cost_usd is not None else None

    event.input_tokens = usage.input_text_tokens
    event.cached_tokens = usage.cached_tokens
    event.output_tokens = usage.output_tokens
    event.cost_usd = cost_usd
    event.price_id = price_id
    event.litellm_cost = Decimal(str(litellm_cost)) if litellm_cost is not None else None
    event.markup_percent = pricing_cfg.markup_percent
    event.usd_rub_rate = pricing_cfg.usd_rub_rate
    event.charged_rub = charged_rub
    event.latency_ms = latency_ms
    event.status = "success"
    event.provider_request_id = provider_request_id
    event.completed_at = utcnow()
    session.add(event)

    payer = await session.get(Customer, event.billing_customer_id, with_for_update=True)
    if charged_rub is not None:
        session.add(
            WalletLedger(
                customer_id=payer.id,
                entry_type="usage",
                delta_rub=-charged_rub,
                usage_event_id=event.id,
            )
        )
        payer.balance_rub -= charged_rub
    await session.commit()
    return payer.balance_rub


async def finalize_failure(
    session: AsyncSession, event: UsageEvent, *, error_code: str, latency_ms: int
) -> None:
    """Транзакция 2 (ошибка провайдера): деньги не списывались — просто
    закрыть событие как failed."""
    event.status = "failed"
    event.error_code = error_code
    event.latency_ms = latency_ms
    event.completed_at = utcnow()
    session.add(event)
    await session.commit()


async def confirm_topup(session: AsyncSession, topup: TopupRequest, admin_id: int) -> Decimal:
    customer = await session.get(Customer, topup.customer_id, with_for_update=True)
    topup.status = "confirmed"
    topup.decided_at = utcnow()
    topup.decided_by_admin_id = admin_id
    session.add(
        WalletLedger(
            customer_id=customer.id,
            entry_type="topup",
            delta_rub=topup.amount_rub,
            topup_request_id=topup.id,
        )
    )
    customer.balance_rub += topup.amount_rub
    await session.commit()
    return customer.balance_rub


async def reject_topup(session: AsyncSession, topup: TopupRequest, admin_id: int) -> None:
    topup.status = "rejected"
    topup.decided_at = utcnow()
    topup.decided_by_admin_id = admin_id
    session.add(topup)
    await session.commit()


async def purchase_subscription(
    session: AsyncSession, customer_id: int, product: Product, account_email: str, note: str | None
) -> SubscriptionOrder:
    """Цена подписки известна заранее (в отличие от API) — списываем сразу,
    как классическую покупку. Дальше это заявка админу на исполнение."""
    price_rub = product.price_rub
    product_id = product.id
    customer = await session.get(Customer, customer_id, with_for_update=True)
    if customer.balance_rub < price_rub:
        # См. комментарий в start_call — намеренно без rollback, чтобы не
        # инвалидировать другие объекты уже загруженные в этой сессии
        # (например customer из auth-зависимости в самом роуте).
        raise InsufficientBalance(customer.balance_rub, required=price_rub)

    order = SubscriptionOrder(
        customer_id=customer_id,
        product_id=product_id,
        account_email=account_email,
        price_rub=price_rub,
        note=note,
    )
    session.add(order)
    await session.flush()

    session.add(
        WalletLedger(
            customer_id=customer_id,
            entry_type="subscription",
            delta_rub=-product.price_rub,
            subscription_order_id=order.id,
        )
    )
    customer.balance_rub -= product.price_rub
    await session.commit()
    return order


async def fulfill_order(session: AsyncSession, order: SubscriptionOrder, admin_id: int) -> None:
    order.status = "fulfilled"
    order.fulfilled_at = utcnow()
    order.decided_by_admin_id = admin_id
    session.add(order)
    await session.commit()


async def refund_order(session: AsyncSession, order: SubscriptionOrder, admin_id: int) -> Decimal:
    customer = await session.get(Customer, order.customer_id, with_for_update=True)
    order.status = "refunded"
    order.fulfilled_at = utcnow()
    order.decided_by_admin_id = admin_id
    session.add(
        WalletLedger(
            customer_id=customer.id,
            entry_type="refund",
            delta_rub=order.price_rub,
            subscription_order_id=order.id,
            note=f"refund order #{order.id}",
        )
    )
    customer.balance_rub += order.price_rub
    await session.commit()
    return customer.balance_rub


_ROYALTY_SHARE = Decimal("0.5")


async def charge_prompt_fee(
    session: AsyncSession, billing_customer_id: int, prompt: Prompt, usage_event_id
) -> None:
    """Списывает фикс. цену промпта сверх обычной токенной стоимости вызова
    (см. finalize_success) и начисляет автору роялти. Списывается по факту,
    как и токенная стоимость — не блокирует вызов заранее."""
    payer = await session.get(Customer, billing_customer_id, with_for_update=True)
    session.add(
        WalletLedger(
            customer_id=payer.id,
            entry_type="usage",
            delta_rub=-prompt.price_rub,
            usage_event_id=usage_event_id,
            note=f"prompt #{prompt.id} fee",
        )
    )
    payer.balance_rub -= prompt.price_rub

    if prompt.author_customer_id != payer.id:
        royalty = (prompt.price_rub * _ROYALTY_SHARE).quantize(_RUB_QUANT)
        author = await session.get(Customer, prompt.author_customer_id, with_for_update=True)
        session.add(
            WalletLedger(
                customer_id=author.id,
                entry_type="adjustment",
                delta_rub=royalty,
                usage_event_id=usage_event_id,
                note=f"royalty prompt #{prompt.id}",
            )
        )
        author.balance_rub += royalty
    await session.commit()
