"""Detect operational problems and notify linked administrators with a cooldown.

The reservation worker schedules these checks. Log output remains available
when the Telegram integration is disabled.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Customer, Resource, TelegramLink, UsageEvent, as_utc, days_left, utcnow
from app.integrations import telegram_bot
from app.services.wallet_reconciliation import find_wallet_mismatches

logger = logging.getLogger(__name__)

_WINDOW = timedelta(hours=1)
_FAILED_CALLS_THRESHOLD = 10
# Одно и то же не повторяем чаще, чем раз в этот срок — иначе при затяжной
# проблеме бот засыплет админа сообщениями каждые две минуты, и их перестанут
# читать. Состояние в памяти: перезапуск процесса сбрасывает — это осознанно,
# после рестарта первое сообщение полезно получить заново.
_COOLDOWN = timedelta(hours=6)


@dataclass
class AlertCooldown:
    """Process-local suppression of repeated operational notifications."""

    window: timedelta = _COOLDOWN
    _last_sent: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def allow(self, key: str, *, now: datetime | None = None) -> bool:
        current = now if now is not None else utcnow()
        last = self._last_sent.get(key)
        if last is not None and current - last < self.window:
            return False
        self._last_sent[key] = current
        return True

    def reset(self) -> None:
        self._last_sent.clear()


default_cooldown = AlertCooldown()


async def _admin_chat_ids(session: AsyncSession) -> list[int]:
    return list(
        (
            await session.execute(
                select(TelegramLink.chat_id)
                .join(Customer, Customer.id == TelegramLink.customer_id)
                .where(Customer.role == "admin", Customer.active)
            )
        )
        .scalars()
        .all()
    )


async def collect_problems(session: AsyncSession) -> list[tuple[str, str]]:
    """(ключ для антиспама, текст сообщения)."""
    since = utcnow() - _WINDOW
    problems: list[tuple[str, str]] = []

    mismatches = await find_wallet_mismatches(session)
    if mismatches:
        # Keep one bounded notification even if an import damaged many wallets.
        # Customer IDs are enough for investigation; do not broadcast emails.
        details = "; ".join(
            f"#{item.customer_id}: {item.difference_rub:+.4f} ₽" for item in mismatches[:10]
        )
        problems.append(
            (
                "wallet_reconciliation",
                f"⚠️ Flawless: баланс расходится с журналом у {len(mismatches)} "
                f"клиентов. Разница balance_rub − wallet_ledger: {details}. "
                "Проверьте операции; автоматическая корректировка не выполняется.",
            )
        )

    uncosted = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.created_at >= since,
                UsageEvent.status == "success",
                UsageEvent.cost_usd.is_(None),
            )
        )
    ).scalar_one()
    if uncosted:
        problems.append(
            (
                "uncosted",
                f"⚠️ Flawless: за час {uncosted} вызовов прошли БЕЗ себестоимости — "
                f"расход у провайдера идёт, а списаний нет. Проверьте строки цен "
                f"в model_prices (возможно, истёк valid_until).",
            )
        )

    failed = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.created_at >= since, UsageEvent.status == "failed"
            )
        )
    ).scalar_one()
    if failed >= _FAILED_CALLS_THRESHOLD:
        problems.append(
            (
                "failed",
                f"⚠️ Flawless: за час {failed} вызовов завершились ошибкой. "
                f"Похоже на проблему у провайдера или с ключами.",
            )
        )

    if settings.enable_resources:
        # Прокси и подписки кончаются молча: узнать об этом в момент, когда
        # всё перестало работать, — худший вариант. Предупреждаем заранее.
        soon = utcnow() + timedelta(days=settings.resource_expiry_warn_days)
        rows = (
            await session.execute(
                select(Resource, Customer.name)
                .join(Customer, Customer.id == Resource.owner_customer_id, isouter=True)
                .where(
                    Resource.archived.is_(False),
                    Resource.expires_at.is_not(None),
                    Resource.expires_at <= soon,
                )
                .order_by(Resource.expires_at)
            )
        ).all()
        now = utcnow()
        for resource, owner_name in rows:
            expires = as_utc(resource.expires_at)
            assert expires is not None
            # Та же функция, что и на страницах: иначе телеграм и кабинет
            # расходятся в оценке одного и того же срока на сутки.
            left = days_left(expires, now)
            assert left is not None
            who = f" ({owner_name})" if owner_name else ""
            if left < 0:
                text = (
                    f"🔴 Flawless: «{resource.name}»{who} ПРОСРОЧЕН "
                    f"{-left} дн. назад — оплачено было до "
                    f"{expires.strftime('%d.%m.%Y')}"
                )
            else:
                when = "сегодня" if left == 0 else f"через {left} дн."
                text = (
                    f"⏳ Flawless: «{resource.name}»{who} истекает {when} — "
                    f"оплачено до {expires.strftime('%d.%m.%Y')}"
                )
            # Ключ с датой окончания: продлили — ключ сменился, и о новом
            # сроке предупредят заново, а не промолчат из-за антиспама.
            problems.append((f"resource:{resource.id}:{expires:%Y-%m-%d}", text))

    return problems


async def check_and_notify(session: AsyncSession, *, cooldown: AlertCooldown | None = None) -> None:
    problems = await collect_problems(session)
    if not problems:
        return

    active_cooldown = cooldown if cooldown is not None else default_cooldown
    fresh = [(key, text) for key, text in problems if active_cooldown.allow(key)]
    for _, text in fresh:
        logger.warning("alert: %s", text)
    if not fresh:
        return

    if not telegram_bot.settings.telegram_bot_token:
        return
    chat_ids = await _admin_chat_ids(session)
    if not chat_ids:
        logger.warning("alert: некому отправить — ни один админ не привязал Telegram")
        return
    for chat_id in chat_ids:
        for _, text in fresh:
            try:
                await telegram_bot.send_message(chat_id, text)
            except Exception as exc:
                # HTTP errors include request URLs containing the bot token.
                logger.warning(
                    "alert: не удалось отправить сообщение в чат %s: %s",
                    chat_id,
                    telegram_bot.safe_error(exc),
                )
