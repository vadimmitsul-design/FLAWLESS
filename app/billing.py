"""Биллинг кошелька в рублях.

В отличие от AI-HUB/gateway (фиксированная цена операции списывается ДО
вызова), здесь клиент сам выбирает модель — точная стоимость известна только
ПОСЛЕ ответа провайдера (зависит от длины completion). Поэтому: перед
вызовом резервируем ОЦЕНОЧНУЮ сумму (см. start_call/1.1 доработок), после —
списываем по факту (себестоимость * наценка * курс), резерв снимается сам —
строка перестаёт быть 'pending'. Баланс может ненадолго уйти в минус на
последнем вызове — следующий вызов уже блокируется той же проверкой. Ledger
append-only, как везде в этом кодовом кусте.
"""

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApiKey,
    Customer,
    ModelPrice,
    PricingConfig,
    Product,
    Prompt,
    SubscriptionOrder,
    TopupRequest,
    UsageEvent,
    WalletLedger,
    utcnow,
)
from app import pricing as pricing_module
from app.pricing import UsageAmounts, estimate_call_cost_usd

logger = logging.getLogger(__name__)

_RUB_QUANT = Decimal("0.0001")


class SpendLimitExceeded(Exception):
    def __init__(self, period: str, scope: str, limit: Decimal, spent: Decimal):
        self.period = period  # daily | monthly
        self.scope = scope  # customer (кошелёк целиком) | key (конкретный API-ключ)
        self.limit = limit
        self.spent = spent
        super().__init__(f"{scope} {period} spend limit exceeded: spent {spent} >= limit {limit}")


class ModelNotPriced(Exception):
    """Для модели нет действующей строки в model_prices на момент вызова.

    Раньше такой вызов проходил и был БЕСПЛАТНЫМ: cost_usd=None ->
    charged_rub=None -> баланс не уменьшался -> проверка «баланс > 0»
    проходила бесконечно. Достаточно было добавить алиас в models.yaml и
    забыть строку цены (или дать истечь valid_until), чтобы расход у
    провайдера шёл, а с клиента не списывалось ничего."""

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        super().__init__(f"no active price row for {provider}/{model}")


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


def _effective_limit(client_limit: Decimal | None, admin_limit: Decimal | None) -> Decimal | None:
    """Админский лимит — потолок ПОВЕРХ клиентского, не замена: действует
    минимум из заданных. Ни один не задан — лимита нет вовсе."""
    if client_limit is None:
        return admin_limit
    if admin_limit is None:
        return client_limit
    return min(client_limit, admin_limit)


async def available_balance(
    session: AsyncSession, billing_customer_id: int, balance_rub: Decimal
) -> Decimal:
    """Баланс за вычетом сумм, зарезервированных под ЕЩЁ выполняющиеся вызовы.

    Единственный расчёт «сколько реально можно потратить прямо сейчас» — им
    обязаны пользоваться ВСЕ списания. Пока эта логика жила только внутри
    start_call, покупка в магазине смотрела на голый balance_rub и не видела
    резервов: во время долгого запроса к модели можно было купить подписку
    почти на весь баланс, обе операции проходили, итог — минус."""
    reserved = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.reserved_rub), 0)).where(
                UsageEvent.billing_customer_id == billing_customer_id,
                UsageEvent.status == "pending",
            )
        )
    ).scalar_one()
    return balance_rub - reserved


async def price_for_call(
    session: AsyncSession, provider: str, model: str, at: datetime
) -> ModelPrice:
    """Цена для вызова, который СОБИРАЕМСЯ сделать. Нет действующей строки —
    отказываемся ДО обращения к провайдеру: лучше явная ошибка «модель
    недоступна», чем тихий бесплатный расход за наш счёт.

    Резервный дефолт в estimate_reserve_rub (fallback_reserve_rub_when_unpriced)
    при этом остаётся — он страхует пути, куда цена может не доехать иначе:
    например, когда fallback-цепочка увела вызов на другую модель уже после
    старта, и её строки цены нет."""
    price = await pricing_module.find_price(session, provider, model, None, None, at)
    if price is None:
        raise ModelNotPriced(provider, model)
    return price


async def _spent_since(
    session: AsyncSession,
    since: datetime,
    *,
    billing_customer_id: int | None = None,
    api_key_id: int | None = None,
) -> Decimal:
    """Сумма РЕАЛЬНО списанного (charged_rub) с начала периода. Не по
    резервам: временный всплеск pending-резервов ложно триггерил бы лимит."""
    stmt = select(func.coalesce(func.sum(UsageEvent.charged_rub), 0)).where(
        UsageEvent.created_at >= since, UsageEvent.charged_rub.is_not(None)
    )
    if billing_customer_id is not None:
        stmt = stmt.where(UsageEvent.billing_customer_id == billing_customer_id)
    if api_key_id is not None:
        stmt = stmt.where(UsageEvent.api_key_id == api_key_id)
    return (await session.execute(stmt)).scalar_one()


async def spent_since(
    session: AsyncSession,
    since: datetime,
    *,
    billing_customer_id: int | None = None,
    api_key_id: int | None = None,
) -> Decimal:
    """То же, что _spent_since, но для внешних вызовов (кабинет, отчёты).
    Отдельное имя, чтобы интерфейс не лез в приватную функцию биллинга и
    формула «сколько потрачено» осталась одна на весь сервис."""
    return await _spent_since(
        session, since, billing_customer_id=billing_customer_id, api_key_id=api_key_id
    )


async def _enforce_limits(
    session: AsyncSession,
    scope: str,
    daily_limit: Decimal | None,
    monthly_limit: Decimal | None,
    **filters,
) -> None:
    """Границы периодов — календарные сутки/месяц по UTC."""
    if daily_limit is None and monthly_limit is None:
        return
    now = utcnow()
    if daily_limit is not None:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await _spent_since(session, day_start, **filters)
        if spent >= daily_limit:
            raise SpendLimitExceeded("daily", scope, daily_limit, spent)
    if monthly_limit is not None:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = await _spent_since(session, month_start, **filters)
        if spent >= monthly_limit:
            raise SpendLimitExceeded("monthly", scope, monthly_limit, spent)


async def check_spend_limits(
    session: AsyncSession, billing_customer_id: int, api_key: ApiKey | None = None
) -> None:
    """ЕДИНАЯ проверка потолков расхода для всех трёх дверей (API, веб-чат,
    Telegram) — одна точка вызова на вход, чтобы дверь нельзя было добавить,
    забыв про лимит (именно так и появился пробел: проверка жила только в
    /v1/chat/completions).

    Два уровня, оба должны пройти:
      - на КОШЕЛЬКЕ (billing_customer_id) — потолок на человека, ставит админ.
        Не обходится ни вторым ключом, ни переходом в чат: считается по всем
        событиям кошелька независимо от того, каким путём пришёл вызов.
        Для детского аккаунта это кошелёк родителя — он и платит;
      - на КЛЮЧЕ, если вызов пришёл по API-ключу: более узкая рамка внутри
        общего потолка (например, отдельный ключ для CI с малым лимитом).
    """
    payer = await session.get(Customer, billing_customer_id)
    if payer is not None:
        await _enforce_limits(
            session,
            "customer",
            payer.daily_limit_rub,
            payer.monthly_limit_rub,
            billing_customer_id=payer.id,
        )
    if api_key is not None:
        await _enforce_limits(
            session,
            "key",
            _effective_limit(api_key.daily_limit_rub, api_key.admin_daily_limit_rub),
            _effective_limit(api_key.monthly_limit_rub, api_key.admin_monthly_limit_rub),
            api_key_id=api_key.id,
        )


async def find_event_by_idempotency_key(
    session: AsyncSession, actor_customer_id: int, idempotency_key: str
) -> UsageEvent | None:
    """По actor'у (customer_id), НЕ по billing_customer_id — иначе два разных
    ребёнка одного родителя делили бы одно пространство ключей идемпотентности
    и могли бы получить чужой кэшированный ответ (находка состязательного
    ревью 2026-09-04)."""
    return (
        await session.execute(
            select(UsageEvent).where(
                UsageEvent.customer_id == actor_customer_id,
                UsageEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


def estimate_reserve_rub(
    price: ModelPrice | None,
    messages: list[dict],
    extra: dict,
    pricing_cfg: PricingConfig,
    extra_fixed_rub: Decimal = Decimal(0),
) -> Decimal:
    """Резерв под вызов (1.1) — с запасом (settings.reserve_safety_margin),
    покрывающим известную недооценку грубой эвристики (~4 симв/токен занижает
    не-латинские языки типа кириллицы, основного языка клиентов) и премию за
    запись в кэш, которую оценка не учитывает по составу. Если прайса вообще
    нет — НЕ считаем резерв нулевым (это отключает защиту резервом целиком
    именно для непроцененной модели, ровно как найдено состязательным ревью
    2026-09-04) — берём консервативный дефолт settings.fallback_reserve_rub_when_unpriced.
    extra_fixed_rub — для фиксированных доплат сверх токенной стоимости
    (например Prompt.price_rub), чтобы такие доплаты тоже попадали под
    защиту резервом, а не списывались бесконтрольно постфактум."""
    cost_usd = estimate_call_cost_usd(price, messages, extra) if price is not None else None
    if cost_usd is not None:
        base_rub = price_in_rub(cost_usd, pricing_cfg)
    else:
        base_rub = Decimal(str(settings.fallback_reserve_rub_when_unpriced))
    margin = Decimal(str(settings.reserve_safety_margin))
    return (base_rub * margin + extra_fixed_rub).quantize(_RUB_QUANT)


async def estimate_reserve_for_chain(
    session: AsyncSession,
    alias: str,
    messages: list[dict],
    extra: dict,
    pricing_cfg: PricingConfig,
    now: datetime,
    extra_fixed_rub: Decimal = Decimal(0),
) -> Decimal:
    """Резерв по самой дорогой модели, которая МОЖЕТ ответить.

    Резервировали по цене запрошенной модели, а списывали по цене той, что
    фактически ответила. Цепочка по умолчанию ведёт gpt-5-mini (0.25/2.00)
    на gemini-flash (1.50/9.00) — в 4,5 раза дороже по выводу. При массовом
    rate limit у поставщика на дорогую модель переезжают все клиенты разом,
    каждый со своим заниженным резервом, и балансы уходят в минус именно
    тогда, когда это хуже всего.

    Цепочка известна ДО вызова, так что это просто максимум по нескольким
    строкам прайса.
    """
    from app import llm  # локальный импорт: llm импортирует pricing, не billing

    best = None
    for candidate in llm.fallback_chain(alias):
        try:
            provider, model = llm.resolve_alias(candidate)
        except KeyError:
            continue
        price = await pricing_module.find_price(session, provider, model, None, None, now)
        if price is None or price.price_per_1m_input_tokens is None:
            continue
        reserve = estimate_reserve_rub(price, messages, extra, pricing_cfg, extra_fixed_rub)
        if best is None or reserve > best:
            best = reserve
    if best is None:
        # Ни у одной модели цепочки нет цены. Вызов всё равно будет отклонён
        # выше (price_for_call), но резерв обязан остаться консервативным.
        best = estimate_reserve_rub(None, messages, extra, pricing_cfg, extra_fixed_rub)
    return best


async def start_call(
    session: AsyncSession,
    actor_customer_id: int,
    billing_customer_id: int,
    provider: str,
    model: str,
    estimated_reserve_rub: Decimal,
    idempotency_key: str | None = None,
) -> UsageEvent:
    """Транзакция 1: проверить баланс ПЛАТЕЛЬЩИКА (1.1 доработок — с учётом
    уже зарезервированного под ДРУГИЕ ещё не завершённые вызовы того же
    плательщика, не только balance_rub > 0 — иначе N параллельных запросов
    проходят каждый по отдельности, суммарно уводя баланс далеко в минус),
    создать pending-событие с actor_customer_id (кто реально вызвал) отдельно
    от billing_customer_id (с чьего баланса спишется — для детских аккаунтов
    это родитель). Коммитит сама.

    UNIQUE(customer_id, idempotency_key) в схеме — при гонке двух запросов
    с одним и тем же ключом ровно один пройдёт INSERT, второй получит
    IntegrityError; main.py ловит его и отвечает 409 "повторите" — НЕ читает
    выигравшую строку в том же запросе (rollback() посреди запроса экспайрит
    все объекты сессии, включая customer из auth-зависимости — тот же урок,
    что и с InsufficientBalance выше). Предварительный find_event_by_idempotency_key
    не закрывает эту гонку (TOCTOU), только страхует типичный случай."""
    payer = await session.get(Customer, billing_customer_id, with_for_update=True)
    available = await available_balance(session, billing_customer_id, payer.balance_rub)
    if payer.balance_rub <= 0 or available < estimated_reserve_rub:
        # Намеренно НЕ делаем rollback здесь: он экспайрит вообще все объекты
        # в сессии (включая customer из Depends(get_current_customer) и любые
        # другие уже загруженные в этом запросе) — следующее же обращение к
        # их атрибутам роняет MissingGreenlet. FOR UPDATE-лок снимется сам,
        # когда FastAPI закроет сессию в конце запроса — это безопаснее.
        raise InsufficientBalance(available, required=estimated_reserve_rub)

    event = UsageEvent(
        customer_id=actor_customer_id,
        billing_customer_id=billing_customer_id,
        provider=provider,
        model=model,
        status="pending",
        reserved_rub=estimated_reserve_rub,
        idempotency_key=idempotency_key,
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
    response_snapshot: str | None = None,
    response_status_code: int | None = None,
    billing_estimated: bool = False,
) -> Decimal:
    """Транзакция 2 (успех): списать по факту, вернуть новый баланс.

    billing_estimated=True — цифры не подтверждены поставщиком, а посчитаны
    нами по тексту ответа (провайдер не прислал usage). Пометка нужна сверке:
    иначе оценку не отличить от подтверждённых данных.
    response_snapshot/response_status_code — для идемпотентного повтора
    (1.4 доработок): main.py передаёт JSON тела ответа, чтобы вернуть его же
    клиенту при повторе с тем же Idempotency-Key, не вызывая провайдера снова."""
    charged_rub = price_in_rub(cost_usd, pricing_cfg) if cost_usd is not None else None

    event.input_tokens = usage.input_text_tokens
    event.cached_tokens = usage.cached_tokens
    event.cache_write_tokens = usage.cache_write_tokens
    event.output_tokens = usage.output_tokens
    event.cost_usd = cost_usd
    event.billing_estimated = billing_estimated
    event.price_id = price_id
    event.litellm_cost = Decimal(str(litellm_cost)) if litellm_cost is not None else None
    event.markup_percent = pricing_cfg.markup_percent
    event.usd_rub_rate = pricing_cfg.usd_rub_rate
    event.charged_rub = charged_rub
    event.latency_ms = latency_ms
    event.status = "success"
    event.provider_request_id = provider_request_id
    event.response_snapshot = response_snapshot
    event.response_status_code = response_status_code
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
    session: AsyncSession,
    event: UsageEvent,
    *,
    error_code: str,
    latency_ms: int,
    usage: UsageAmounts | None = None,
    cost_usd: Decimal | None = None,
    price_id: int | None = None,
    pricing_cfg: PricingConfig | None = None,
    estimated: bool = False,
    response_snapshot: str | None = None,
    response_status_code: int | None = None,
) -> Decimal | None:
    """Транзакция 2 (ошибка/обрыв): по умолчанию деньги не списывались —
    просто закрыть событие как failed. НО если поток успел отдать клиенту
    часть ответа ДО обрыва (usage/cost_usd переданы — см. 1.3 доработок),
    списываем по оценке этого объёма: иначе клиент получает бесплатный
    частичный ответ, просто оборвав соединение. estimated=True в этом
    случае — сверка (1.5) должна отличать это от подтверждённого usage."""
    event.status = "failed"
    event.error_code = error_code
    event.latency_ms = latency_ms
    event.response_snapshot = response_snapshot
    event.response_status_code = response_status_code
    event.completed_at = utcnow()

    charged_rub = price_in_rub(cost_usd, pricing_cfg) if cost_usd is not None and pricing_cfg is not None else None
    if usage is not None and charged_rub is not None:
        event.input_tokens = usage.input_text_tokens
        event.cached_tokens = usage.cached_tokens
        event.cache_write_tokens = usage.cache_write_tokens
        event.output_tokens = usage.output_tokens
        event.cost_usd = cost_usd
        event.price_id = price_id
        event.markup_percent = pricing_cfg.markup_percent
        event.usd_rub_rate = pricing_cfg.usd_rub_rate
        event.charged_rub = charged_rub
        event.billing_estimated = estimated
    session.add(event)

    if charged_rub is not None:
        payer = await session.get(Customer, event.billing_customer_id, with_for_update=True)
        session.add(
            WalletLedger(
                customer_id=payer.id,
                entry_type="usage",
                delta_rub=-charged_rub,
                usage_event_id=event.id,
                note="оценка по обрыву потока" if estimated else None,
            )
        )
        payer.balance_rub -= charged_rub
        await session.commit()
        return payer.balance_rub

    await session.commit()
    return None


async def confirm_topup(
    session: AsyncSession, topup: TopupRequest, admin_id: int
) -> Decimal | None:
    """Зачислить деньги по заявке. None — заявку уже кто-то решил.

    Переход статуса делается атомарным UPDATE с условием, а не присваиванием:
    проверка «ещё requested» в маршруте и запись здесь — это два разных
    момента, и между ними помещается точно такой же второй запрос (двойной
    клик по кнопке, две вкладки админки, два администратора). Оба увидели бы
    requested, оба прошли бы проверку, и клиенту зачислилось бы вдвое больше,
    чем он перевёл. Тем же приёмом гасятся одноразовые коды приглашений.
    """
    claimed = await session.execute(
        update(TopupRequest)
        .where(TopupRequest.id == topup.id, TopupRequest.status == "requested")
        .values(status="confirmed", decided_at=utcnow(), decided_by_admin_id=admin_id)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return None
    customer = await session.get(Customer, topup.customer_id, with_for_update=True)
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


async def reject_topup(session: AsyncSession, topup: TopupRequest, admin_id: int) -> bool:
    """False — заявку уже решили. Та же гонка, что у подтверждения: отказ и
    зачисление, нажатые одновременно, иначе сделали бы и то, и другое."""
    claimed = await session.execute(
        update(TopupRequest)
        .where(TopupRequest.id == topup.id, TopupRequest.status == "requested")
        .values(status="rejected", decided_at=utcnow(), decided_by_admin_id=admin_id)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return False
    await session.commit()
    return True


async def admin_adjust_balance(
    session: AsyncSession,
    customer_id: int,
    delta_rub: Decimal,
    entry_type: str,
    note: str,
    admin_id: int,
) -> Decimal:
    """Прямая операция админа с балансом — без встречной заявки от клиента.
    Раньше баланс можно было пополнить ТОЛЬКО подтвердив заявку, которую
    клиент подал сам: выдать десяти разработчикам месячный бюджет означало
    десять заявок от них и десять подтверждений от админа.

    entry_type различает смысл, а не механику (обе записи одинаково двигают
    баланс), и от него зависят цифры в /admin/overview:
      topup      — реальные деньги пришли, попадает в «Кассу»;
      adjustment — начисление внутреннего бюджета или исправление ошибки,
                   кассой НЕ является.
    Минус разрешён осознанно: после сбоя в биллинге нужно уметь и забрать.
    """
    payer = await session.get(Customer, customer_id, with_for_update=True)
    if payer is None:
        raise ValueError(f"customer {customer_id} not found")
    session.add(
        WalletLedger(
            customer_id=payer.id,
            entry_type=entry_type,
            delta_rub=delta_rub,
            created_by_admin_id=admin_id,
            note=note,
        )
    )
    payer.balance_rub += delta_rub
    await session.commit()
    return payer.balance_rub


async def update_pricing_config(
    session: AsyncSession, markup_percent: Decimal, usd_rub_rate: Decimal, admin_id: int
) -> PricingConfig:
    """Наценка и курс задним числом ничего не пересчитывают: оба значения
    копируются в UsageEvent в момент вызова, поэтому влияют только на будущие
    вызовы. Раньше правились единственным способом — SQL в боевой базе."""
    cfg = await get_pricing_config(session)
    cfg.markup_percent = markup_percent
    cfg.usd_rub_rate = usd_rub_rate
    cfg.updated_by_admin_id = admin_id
    await session.commit()
    return cfg


async def purchase_subscription(
    session: AsyncSession, customer_id: int, product: Product, account_email: str, note: str | None
) -> SubscriptionOrder:
    """Цена подписки известна заранее (в отличие от API) — списываем сразу,
    как классическую покупку. Дальше это заявка админу на исполнение."""
    price_rub = product.price_rub
    product_id = product.id
    customer = await session.get(Customer, customer_id, with_for_update=True)
    # По ДОСТУПНОМУ балансу, а не по голому balance_rub: иначе покупка не
    # видит денег, уже зарезервированных под выполняющиеся вызовы, и те же
    # рубли тратятся дважды.
    available = await available_balance(session, customer_id, customer.balance_rub)
    if available < price_rub:
        # См. комментарий в start_call — намеренно без rollback, чтобы не
        # инвалидировать другие объекты уже загруженные в этой сессии
        # (например customer из auth-зависимости в самом роуте).
        raise InsufficientBalance(available, required=price_rub)

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
    (см. finalize_success) и начисляет автору роялти.

    Два правила, без которых это работало как насос для чужого кошелька:

    1. ВНУТРИ ОДНОГО КОШЕЛЬКА промпт бесплатен — ни платы, ни роялти.
       Старая проверка сравнивала author.id с payer.id и пропускала связку
       «ребёнок-автор, платит родитель»: ребёнок публиковал промпт за 100 ₽,
       вызывал его своим ключом, родитель терял 100 ₽, ребёнку приходило 50 ₽.
       В цикле кошелёк родителя вычерпывался (воспроизведено на живом
       приложении в аудите 2026-09-07). Сравниваем теперь КОШЕЛЬКИ, а не
       аккаунты, и в этом случае не берём даже саму плату — платить самому
       себе за свой же промпт смысла нет.

    2. РОЯЛТИ ВЫПЛАЧИВАЕТСЯ ТОЛЬКО ИЗ СОБРАННЫХ ДЕНЕГ. Если после списания
       платы баланс плательщика уходит в минус, значит денег на неё не было —
       и раздавать автору половину того, чего мы не получили, нельзя. Плату
       всё равно фиксируем (услуга оказана, долг виден в журнале), роялти —
       нет, с записью в лог.
    """
    # Блокируем ОБА аккаунта сразу, в одном и том же порядке — по возрастанию
    # id — независимо от того, кто в этом вызове платит, а кто автор. Иначе
    # вызов A (платит X, автор Y) и встречный вызов B (платит Y, автор X),
    # выполняясь одновременно, блокируют те же две строки в ПРОТИВОПОЛОЖНОМ
    # порядке: A держит X и ждёт Y, B держит Y и ждёт X — классический
    # deadlock, Postgres обрывает одну из двух транзакций с ошибкой. Порядок
    # берём по «сырым» id (billing_customer_id / prompt.author_customer_id),
    # не по разрешённому кошельку, — дальше по коду мутируется и читается
    # именно строка автора, а не его родитель.
    author_id = prompt.author_customer_id
    if author_id == billing_customer_id:
        payer = await session.get(Customer, billing_customer_id, with_for_update=True)
        author = payer
    elif author_id < billing_customer_id:
        author = await session.get(Customer, author_id, with_for_update=True)
        payer = await session.get(Customer, billing_customer_id, with_for_update=True)
    else:
        payer = await session.get(Customer, billing_customer_id, with_for_update=True)
        author = await session.get(Customer, author_id, with_for_update=True)
    if author is not None and resolve_billing_customer_id(author) == billing_customer_id:
        return

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
    # Плата должна попасть и в СУММУ СОБЫТИЯ, а не только в журнал: вся
    # арифметика «сколько человек потратил» построена на charged_rub —
    # потолки на кошелёк и на ключ, отчёт для бухгалтерии, карточка человека,
    # сводка в кабинете клиента. Пока плата шла мимо, бюджетный потолок не
    # ограничивал расход на промпты вообще, а клиент видел у себя почти
    # нулевой расход при вычерпанном кошельке.
    event = await session.get(UsageEvent, usage_event_id)
    if event is not None:
        event.charged_rub = (event.charged_rub or Decimal(0)) + prompt.price_rub

    if author is not None and payer.balance_rub >= 0:
        # author уже заблокирован выше, вместе с payer, — второй раз брать
        # лок здесь не нужно (и в старой редакции именно это лишнее
        # обращение стояло не в общем порядке с payer, отсюда и deadlock).
        royalty = (prompt.price_rub * _ROYALTY_SHARE).quantize(_RUB_QUANT)
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
    elif author is not None:
        logger.warning(
            "prompt #%s: роялти не начислено — плата увела баланс плательщика %s в минус (%s ₽)",
            prompt.id,
            payer.id,
            payer.balance_rub,
        )
    await session.commit()
