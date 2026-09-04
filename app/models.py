import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CUSTOMER_ROLES = ("customer", "admin")
TOPUP_STATUSES = ("requested", "confirmed", "rejected")
USAGE_STATUSES = ("pending", "success", "failed")
WALLET_ENTRY_TYPES = ("topup", "usage", "subscription", "refund", "adjustment")
ORDER_STATUSES = ("paid", "fulfilled", "refunded")
PASSWORD_RESET_STATUSES = ("requested", "completed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    """Клиент платформы. role='admin' подтверждает пополнения баланса,
    остальные — обычные покупатели токенов. balance_rub — материализованный
    остаток, меняется только вместе с записью в WalletLedger.
    """

    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("role IN ('customer','admin')", name="ck_customers_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="customer", server_default="customer")
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_child: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    parent_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApiKey(Base):
    """Ключ для /v1/chat/completions (Authorization: Bearer <ключ>). У клиента
    может быть несколько именованных ключей (2.2 доработок) — каждый со
    своими лимитами расхода. last_four — последние 4 символа сырого ключа для
    опознания в списке; сам ключ по хэшу не восстановить."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    name: Mapped[str] = mapped_column(Text, default="", server_default="")
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    last_four: Mapped[str] = mapped_column(Text, default="", server_default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Лимиты расхода (2.2): клиент настраивает свои daily/monthly_limit_rub в
    # кабинете; admin_*_limit_rub — потолок админа поверх (не заменяет
    # клиентский, действует минимум из заданных — см. billing._effective_limit).
    daily_limit_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    monthly_limit_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    admin_daily_limit_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    admin_monthly_limit_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelPrice(Base):
    """Себестоимость у провайдера — БЕЗ наценки (наценка в PricingConfig).
    Схема идентична gateway/AI-HUB: строки с периодом действия, задним
    числом не редактируются (закрыть valid_until, добавить новую).
    """

    __tablename__ = "model_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    quality: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    price_per_image: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    price_per_1m_input_tokens: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    price_per_1m_input_image_tokens: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    price_per_1m_output_tokens: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    price_per_1m_cached_tokens: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # Запись в кэш (Anthropic prompt caching) — обычно ДОРОЖЕ обычного input,
    # в отличие от чтения кэша (price_per_1m_cached_tokens). Раздельно от
    # чтения намеренно — иначе на длинных кэшированных контекстах прямой убыток.
    price_per_1m_cache_write_tokens: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str] = mapped_column(Text, default="USD", server_default="USD")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PricingConfig(Base):
    """Одна строка (id=1): наценка % и курс USD->RUB для перевода
    себестоимости в цену клиента. Правится админом, не кодом/редеплоем.
    """

    __tablename__ = "pricing_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    usd_rub_rate: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )


class UsageEvent(Base):
    """Один вызов /v1/chat/completions. Себестоимость и наценка/курс на
    момент вызова фиксируются здесь же — задним числом не пересчитываются.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending','success','failed')", name="ck_usage_events_status"),
        # По actor'у (customer_id), не по billing_customer_id: два разных
        # ребёнка одного родителя делят billing_customer_id, но не должны
        # видеть чужой ответ при случайном совпадении ключа (см. CLAUDE.md,
        # находка состязательного ревью 2026-09-04).
        UniqueConstraint("customer_id", "idempotency_key", name="uq_usage_events_customer_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    billing_customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    # NULL для вызовов не через /v1/chat/completions (Telegram-секретарь —
    # там нет API-ключа вообще). Нужен для лимитов расхода НА КЛЮЧ (2.2).
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id"), index=True)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    price_id: Mapped[int | None] = mapped_column(ForeignKey("model_prices.id"))
    litellm_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    markup_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    usd_rub_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    charged_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    # Оценка (1.1/1.3 доработок): сколько зарезервировано под ЕЩЁ выполняющийся
    # вызов (status='pending') — по max_tokens/эвристике, ДО фактического
    # ответа провайдера. Как только статус меняется на success/failed, строка
    # перестаёт учитываться в сумме активных резервов сама по себе — отдельный
    # шаг "снять резерв" не нужен.
    reserved_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    # True — charged_rub посчитан НЕ по подтверждённому usage от провайдера, а
    # по нашей оценке (см. billing.finalize_failure): обрыв стрима после того,
    # как клиенту уже ушла часть ответа. Помечаем отдельно, чтобы сверка (1.5)
    # не путала это с обычным success.
    billing_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    dlp_redactions: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    # Идемпотентность (1.4 доработок): клиент передаёт Idempotency-Key, повтор
    # с тем же ключом возвращает response_snapshot/response_status_code
    # первой попытки вместо повторного вызова провайдера и списания.
    # UNIQUE(customer_id, idempotency_key) — NULL не участвует в уникальности
    # (и в Postgres, и в SQLite), так что обычные вызовы без ключа никак не
    # ограничены.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    # Хэш (model, messages, prompt_id) исходного запроса — при повторе с тем
    # же ключом, но ДРУГИМ телом запроса, отдаём 409, а не чужой кэшированный
    # ответ молча (находка состязательного ревью 2026-09-04).
    idempotency_request_hash: Mapped[str | None] = mapped_column(Text)
    response_snapshot: Mapped[str | None] = mapped_column(Text)
    response_status_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TopupRequest(Base):
    """Заявка клиента на пополнение баланса. Оплата пока ручная (перевод/
    счёт вне системы) — админ подтверждает поступление денег, тогда
    создаётся WalletLedger(entry_type=topup) и растёт balance_rub. Реальный
    эквайринг подключится сюда же позже, без смены модели."""

    __tablename__ = "topup_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','confirmed','rejected')", name="ck_topup_requests_status"
        ),
        CheckConstraint("amount_rub > 0", name="ck_topup_requests_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(Text, default="requested", server_default="requested")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))


class PasswordResetRequest(Base):
    """Заявка на сброс пароля — по аналогии с TopupRequest: без email-
    рассылки самостоятельный сброс небезопасен (кто угодно мог бы сбросить
    чужой пароль по email), поэтому админ вручную генерирует новый пароль и
    передаёт клиенту любым доступным каналом вне системы."""

    __tablename__ = "password_reset_requests"
    __table_args__ = (
        CheckConstraint("status IN ('requested','completed')", name="ck_password_reset_requests_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(Text, default="requested", server_default="requested")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))


class Product(Base):
    """Каталог платных подписок (ChatGPT Plus, Claude Pro и т.п.), которые
    можно купить с баланса кошелька. Оплата пока не сама подписка у
    провайдера — это фиксация покупки, исполнение (покупка карты, ввод в
    аккаунт клиента) делает админ вручную, см. SubscriptionOrder."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubscriptionOrder(Base):
    """Заказ подписки. Деньги списываются с баланса сразу при заказе (цена
    известна заранее, в отличие от API-биллинга) — дальше админ вручную
    оплачивает у провайдера и вводит в account_email клиента, отмечает
    fulfilled. Если не удалось исполнить — refunded возвращает деньги."""

    __tablename__ = "subscription_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('paid','fulfilled','refunded')", name="ck_subscription_orders_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    account_email: Mapped[str] = mapped_column(Text)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(Text, default="paid", server_default="paid")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))


class WalletLedger(Base):
    """Append-only журнал баланса. Никаких UPDATE/DELETE — исправление
    ошибки оформляется компенсирующей записью (adjustment/refund)."""

    __tablename__ = "wallet_ledger"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('topup','usage','subscription','refund','adjustment')",
            name="ck_wallet_ledger_entry_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    entry_type: Mapped[str] = mapped_column(Text)
    delta_rub: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    usage_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usage_events.id"))
    topup_request_id: Mapped[int | None] = mapped_column(ForeignKey("topup_requests.id"))
    subscription_order_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_orders.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Prompt(Base):
    """Промпт-«роль» в библиотеке промптов. Покупается не сам текст, а факт
    генерации: price_rub списывается сверх обычной токенной стоимости
    вызова, автору начисляется роялти (см. billing.charge_prompt_fee)."""

    __tablename__ = "prompts"
    __table_args__ = (CheckConstraint("price_rub > 0", name="ck_prompts_price_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DialogueArchive(Base):
    """Хэш-сертификация диалога с ИИ — доказательство существования на
    момент времени. Хранится только хэш и метка, НЕ содержимое (иначе сам
    архив стал бы хранилищем чувствительных данных)."""

    __tablename__ = "dialogue_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    content_hash: Mapped[str] = mapped_column(Text, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TelegramLinkCode(Base):
    """Одноразовый код привязки: клиент получает его в кабинете и
    отправляет боту '/start КОД'. Удаляется сразу после использования."""

    __tablename__ = "telegram_link_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TelegramLink(Base):
    """Привязка аккаунта neurohub к Telegram-чату. Один активный чат на
    клиента — повторная привязка перезаписывает chat_id."""

    __tablename__ = "telegram_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
