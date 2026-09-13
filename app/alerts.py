"""Оповещения администраторам о том, что сервис нездоров.

До этого наблюдаемость была такая: строчка в логе и две HTML-страницы, на
которые надо специально пойти и посмотреть. Про то, что вызовы идут без
себестоимости (то есть расход у провайдера есть, а списаний нет) или что
провайдер массово отвечает ошибками, узнавали бы от пользователей.

Отдельного канала уведомлений заводить не стали — используем уже
работающего Telegram-бота: администраторы, привязавшие аккаунт, получают
сообщение. Проверки выполняются в том же цикле, что и уборщик зависших
резервов (app/reaper.py), отдельная фоновая задача не нужна.
"""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Customer, Resource, TelegramLink, UsageEvent, as_utc, utcnow

logger = logging.getLogger(__name__)

_WINDOW = timedelta(hours=1)
_FAILED_CALLS_THRESHOLD = 10
# Одно и то же не повторяем чаще, чем раз в этот срок — иначе при затяжной
# проблеме бот засыплет админа сообщениями каждые две минуты, и их перестанут
# читать. Состояние в памяти: перезапуск процесса сбрасывает — это осознанно,
# после рестарта первое сообщение полезно получить заново.
_COOLDOWN = timedelta(hours=6)
_last_sent: dict[str, object] = {}


def _cooled_down(key: str) -> bool:
    last = _last_sent.get(key)
    if last is not None and utcnow() - last < _COOLDOWN:
        return False
    _last_sent[key] = utcnow()
    return True


async def _admin_chat_ids(session: AsyncSession) -> list[int]:
    return list(
        (
            await session.execute(
                select(TelegramLink.chat_id)
                .join(Customer, Customer.id == TelegramLink.customer_id)
                .where(Customer.role == "admin", Customer.active)
            )
        ).scalars().all()
    )


async def _collect_problems(session: AsyncSession) -> list[tuple[str, str]]:
    """(ключ для антиспама, текст сообщения)."""
    since = utcnow() - _WINDOW
    problems: list[tuple[str, str]] = []

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
            left = (expires - now).days
            who = f" ({owner_name})" if owner_name else ""
            if left < 0:
                text = (
                    f"🔴 Flawless: «{resource.name}»{who} ПРОСРОЧЕН "
                    f"{-left} дн. назад — оплачено было до "
                    f"{expires.strftime('%d.%m.%Y')}"
                )
            else:
                text = (
                    f"⏳ Flawless: «{resource.name}»{who} истекает через {left} дн. — "
                    f"оплачено до {resource.expires_at.strftime('%d.%m.%Y')}"
                )
            # Ключ с датой окончания: продлили — ключ сменился, и о новом
            # сроке предупредят заново, а не промолчат из-за антиспама.
            problems.append((f"resource:{resource.id}:{expires:%Y-%m-%d}", text))

    return problems


async def check_and_notify(session: AsyncSession) -> None:
    problems = await _collect_problems(session)
    if not problems:
        return

    fresh = [(key, text) for key, text in problems if _cooled_down(key)]
    for _, text in fresh:
        logger.warning("alert: %s", text)
    if not fresh:
        return

    # Импорт внутри функции: telegram_bot импортирует chatcore -> billing,
    # на уровне модуля это дало бы цикл.
    from app import telegram_bot

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
            except Exception:
                logger.exception("alert: не удалось отправить сообщение в чат %s", chat_id)
