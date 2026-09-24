"""AI-секретарь через Telegram. Long polling (getUpdates), а не webhook —
сервис пока не имеет публичного HTTPS-адреса, а polling — это исходящие
запросы наружу, которые работают откуда угодно, включая локальную машину.
Когда появится публичный домен, можно будет переключить на setWebhook, не
меняя логику обработки сообщений (_handle_update)."""

import asyncio
import io
import logging
import os

import httpx
import litellm
from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.db.models import Customer, TelegramLink, TelegramLinkCode
from app.services import billing
from app.services import telegram_chat as chatcore

logger = logging.getLogger(__name__)

bot_username: str | None = None
_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
_FILE_API = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}"


def safe_error(e: BaseException) -> str:
    """Текст ошибки без токена бота.

    httpx кладёт в сообщение об ошибке полный URL запроса, а в URL Telegram
    токен стоит прямо в пути (/bot<ТОКЕН>/getUpdates). Любой
    logger.warning("... %r", e) на неудачном вызове writes боевой токен в
    лог приложения — оттуда он уезжает в любую систему сбора логов и в
    вывод `docker logs`.
    """
    text = f"{type(e).__name__}: {e}"
    token = settings.telegram_bot_token
    if token:
        text = text.replace(token, "<ТОКЕН-СКРЫТ>")
    return text


async def _api_call(method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.get(f"{_API}/{method}", params=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram api error on {method}: {data}")
        return data["result"]


_TELEGRAM_MESSAGE_LIMIT = 4000  # у Telegram 4096, оставляем запас под разметку


async def send_message(chat_id: int, text: str) -> None:
    """Длинный ответ уходит НЕСКОЛЬКИМИ сообщениями, а не обрезается.

    Раньше text[:4000] молча отбрасывал хвост: человек платил за полный
    ответ модели, получал обрубок на полуслове и не знал, что ответ
    продолжался. Режем по границам строк, чтобы код и списки не рвались
    посреди символа.
    """
    chunks: list[str] = []
    rest = text or ""
    while len(rest) > _TELEGRAM_MESSAGE_LIMIT:
        cut = rest.rfind("\n", 0, _TELEGRAM_MESSAGE_LIMIT)
        if cut <= 0:
            cut = _TELEGRAM_MESSAGE_LIMIT
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)

    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            if not chunk:
                continue
            await client.post(f"{_API}/sendMessage", json={"chat_id": chat_id, "text": chunk})


async def _transcribe_voice(file_id: str) -> str:
    file_info = await _api_call("getFile", file_id=file_id)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{_FILE_API}/{file_info['file_path']}")
        r.raise_for_status()
        audio_bytes = r.content
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "voice.ogg"
    transcript = await litellm.atranscription(
        model="whisper-1", file=audio_file, api_key=os.environ.get("OPENAI_API_KEY")
    )
    return transcript.text


async def _handle_update(update: dict) -> None:
    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text")
    voice = message.get("voice")

    async with SessionLocal() as session:
        if text and text.startswith("/start"):
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            if not code:
                await send_message(
                    chat_id,
                    "Привет! Я AI-секретарь Flawless. Получите код привязки в личном кабинете "
                    "(раздел «Telegram-секретарь») и отправьте: /start КОД",
                )
                return
            link_code = (
                await session.execute(select(TelegramLinkCode).where(TelegramLinkCode.code == code))
            ).scalar_one_or_none()
            if link_code is None:
                await send_message(chat_id, "Код не найден или уже использован.")
                return
            existing = (
                await session.execute(
                    select(TelegramLink).where(TelegramLink.customer_id == link_code.customer_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.chat_id = chat_id
            else:
                session.add(TelegramLink(customer_id=link_code.customer_id, chat_id=chat_id))
            await session.delete(link_code)
            await session.commit()
            await send_message(
                chat_id, "Готово! Аккаунт подключён — пишите или наговаривайте вопрос."
            )
            return

        link = (
            await session.execute(select(TelegramLink).where(TelegramLink.chat_id == chat_id))
        ).scalar_one_or_none()
        if link is None:
            await send_message(
                chat_id,
                "Аккаунт не подключён. Получите код в личном кабинете Flawless и отправьте /start КОД",
            )
            return

        customer = await session.get(Customer, link.customer_id)
        if customer is None or not customer.active:
            await send_message(chat_id, "Аккаунт недоступен.")
            return

        prefix = ""
        if voice:
            # Распознавание — ПЛАТНЫЙ вызов Whisper нашим ключом, и раньше он
            # шёл до единственной проверки: до ограничителя частоты, до
            # потолков расхода, до проверки баланса (всё это живёт внутри
            # run_chat_turn, строкой ниже). Человек с нулевым балансом мог
            # слать голосовые бесконечно, счёт OpenAI рос, а в отчётах этого
            # расхода не видно вовсе — usage_event на распознавание не
            # создаётся.
            #
            # Стоимость распознавания по-прежнему НЕ биллится: для этого нужна
            # строка цены за минуту аудио, которой в model_prices нет. Здесь
            # закрыт бесплатный поток от того, кто заведомо не платит.
            try:
                await chatcore.ensure_can_spend(session, customer)
            except billing.InsufficientBalance:
                await send_message(
                    chat_id,
                    "Недостаточно средств на балансе — пополните в личном кабинете Flawless. "
                    "Голосовые не распознаются, пока баланс пуст.",
                )
                return
            except billing.SpendLimitExceeded as e:
                await send_message(
                    chat_id,
                    f"Достигнут лимит расхода ({'дневной' if e.period == 'daily' else 'месячный'}): "
                    f"потрачено {e.spent} ₽ из {e.limit} ₽. Лимит меняет администратор.",
                )
                return
            except chatcore.TooManyRequests:
                await send_message(chat_id, "Слишком часто — подождите немного и повторите.")
                return
            try:
                user_text = await _transcribe_voice(voice["file_id"])
            except Exception as e:
                logger.warning("whisper transcription failed: %s", safe_error(e))
                await send_message(
                    chat_id, "Не удалось распознать голосовое сообщение, попробуйте ещё раз."
                )
                return
            prefix = f"🎙 Распознано: {user_text}\n\n"
        elif text:
            user_text = text
        else:
            await send_message(
                chat_id, "Пока поддерживаются только текстовые и голосовые сообщения."
            )
            return

        try:
            reply = await chatcore.run_chat_turn(
                session,
                customer.id,
                settings.telegram_default_model,
                [{"role": "user", "content": user_text}],
            )
        except billing.InsufficientBalance:
            await send_message(
                chat_id,
                f"{prefix}Недостаточно средств на балансе — пополните в личном кабинете Flawless.",
            )
            return
        except billing.SpendLimitExceeded as e:
            await send_message(
                chat_id,
                f"{prefix}Достигнут лимит расхода ({'дневной' if e.period == 'daily' else 'месячный'}): "
                f"потрачено {e.spent} ₽ из {e.limit} ₽. Лимит меняет администратор.",
            )
            return
        except chatcore.EmptyProviderResponse:
            await send_message(
                chat_id,
                f"{prefix}Модель не дала ответа на этот запрос — деньги не списаны, "
                f"попробуйте переформулировать.",
            )
            return
        except chatcore.TooManyRequests:
            await send_message(chat_id, f"{prefix}Слишком часто — подождите немного и повторите.")
            return
        except billing.ModelNotPriced:
            logger.error("telegram: no active price row for the configured model")
            await send_message(chat_id, f"{prefix}Модель временно недоступна — уже разбираемся.")
            return
        except Exception as e:
            logger.warning("telegram chat turn failed: %r", e)
            await send_message(chat_id, f"{prefix}Провайдер сейчас недоступен, попробуйте ещё раз.")
            return

        await send_message(chat_id, f"{prefix}{reply}")


async def poll_loop() -> None:
    global bot_username
    try:
        me = await _api_call("getMe")
        bot_username = me.get("username")
        logger.info("telegram bot polling started (@%s)", bot_username)
    except Exception as e:
        logger.warning(
            "telegram getMe failed, continuing without known username: %s", safe_error(e)
        )

    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            updates = await _api_call("getUpdates", **params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("telegram getUpdates failed: %s", safe_error(e))
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                await _handle_update(update)
            except Exception:
                logger.exception(
                    "telegram update handling failed for update_id=%s", update.get("update_id")
                )
