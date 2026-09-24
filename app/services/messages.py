"""services / messages for the Flawless application."""

import json
import re
from typing import Any

from app.db.models import (
    Customer,
    Prompt,
)
from app.services import dlp

# ---------- детский тариф «Репетитор» ----------

_CHILD_SYSTEM_PROMPT = (
    "Ты — репетитор. Не давай готовый ответ на задание (сочинение, реферат, доклад, готовое решение "
    "целиком) — вместо этого объясняй тему и задавай наводящие вопросы, чтобы ученик пришёл к ответу сам."
)


_CHILD_BLOCKED_PATTERN = re.compile(
    r"(?i)\b(напиши|сделай|составь|сгенерируй)\b[^.]{0,40}\b(сочинение|реферат|эссе|доклад)\b"
)


class ChildRequestBlocked(Exception):
    pass


def prepare_messages(
    customer: Customer, messages: list[dict], prompt: Prompt | None
) -> tuple[list[dict], list[str]]:
    """Готовит messages к отправке провайдеру: детский системный промпт и
    блок-лист (если детский аккаунт), системный промпт купленной «роли»,
    DLP-редактирование секретов. Порядок: сначала детский промпт (внешний
    контроль), затем промпт из библиотеки (пользовательский выбор)."""
    prepared = list(messages)

    if customer.is_child:
        # dlp.message_text, а не isinstance(str): сообщение с картинкой уходит
        # массивом частей, и проверка брала ПРЕДЫДУЩЕЕ строковое сообщение или
        # пустую строку. Ребёнку достаточно было приложить любую картинку,
        # чтобы запрет «напиши сочинение» перестал срабатывать, а платил
        # при этом родитель.
        last_user_text = next(
            (dlp.message_text(m) for m in reversed(prepared) if m.get("role") == "user"),
            "",
        )
        if _CHILD_BLOCKED_PATTERN.search(last_user_text or ""):
            raise ChildRequestBlocked()
        prepared = [{"role": "system", "content": _CHILD_SYSTEM_PROMPT}] + prepared

    if prompt is not None:
        prepared = [{"role": "system", "content": prompt.system_prompt}] + prepared

    prepared, dlp_found = dlp.redact_messages(prepared)
    return prepared, dlp_found


# Парсинг PDF/офисных документов НЕ реализован (нужна доп. библиотека,
# scope не указывал конкретные форматы) — только картинки (vision) и простой
# текст. См. CLAUDE.md.


def chat_message_parts_or_none(content: str) -> list[dict[str, Any]] | None:
    """Список частей OpenAI-формата (текст + картинка) — или None, если это
    обычный текст.

    Одного «[» в начале мало: «[1, 2, 3]» от пользователя — тоже валидный
    JSON-массив. Его разбирало как части, и в истории вместо текста
    показывалось «[вложение]», а провайдеру при КАЖДОМ следующем сообщении
    уходил список чисел вместо строки — вызов падал 502, и диалог ломался
    навсегда: удалить сообщение в интерфейсе нечем. Свой формат узнаём по
    ФОРМЕ: непустой список словарей, у каждого строковый type.
    """
    if not content.startswith("["):
        return None
    try:
        parts = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(parts, list) or not parts:
        return None
    if not all(isinstance(part, dict) and isinstance(part.get("type"), str) for part in parts):
        return None
    return parts


def chat_message_display_text(content: str) -> str:
    """Для истории в интерфейсе достаточно текстовой части."""
    parts = chat_message_parts_or_none(content)
    if parts is None:
        return content
    texts = [part.get("text", "") for part in parts if part.get("type") == "text"]
    return "\n".join(t for t in texts if t) or "[вложение]"


def chat_message_provider_content(content: str) -> str | list[dict[str, Any]]:
    parts = chat_message_parts_or_none(content)
    return content if parts is None else parts
