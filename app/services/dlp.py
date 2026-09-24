"""Грубый DLP-фильтр: вырезает похожее на секреты из промпта ПЕРЕД отправкой
провайдеру. Хранится только СПИСОК СРАБОТАВШИХ ТИПОВ (для видимости в
истории вызовов), не сам секрет — иначе БД сама стала бы хранилищем
чувствительных данных, которые мы же и пытаемся защитить.

Паттерны специально консервативные (требуют контекста типа "ИНН:"/"пароль:")
там, где иначе была бы лавина ложных срабатываний на обычные числа/код.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("api_key_like", re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[a-z])-[A-Za-z0-9_\-]{16,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.]{16,}")),
    ("password_assignment", re.compile(r"(?i)\bpassword\s*[:=]\s*\S{4,}")),
    ("ru_inn_labeled", re.compile(r"(?i)\bИНН\s*[:\s]\s*\d{10,12}\b")),
    ("ru_passport_labeled", re.compile(r"(?i)\bпаспорт\s*[:\s]*\d{4}\s?\d{6}\b")),
]


def redact_text(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for name, pattern in _PATTERNS:

        def _repl(m: re.Match[str], _name: str = name) -> str:
            found.append(_name)
            return f"[REDACTED:{_name}]"

        text = pattern.sub(_repl, text)
    return text, found


def message_text(message: dict[str, object]) -> str:
    """Весь текст сообщения одной строкой — и для строкового content, и для
    массива частей.

    Нужен всем, кто принимает решение по тексту: редактированию секретов и
    детскому блок-листу. Раньше каждый смотрел только на строку, и достаточно
    было приложить картинку, чтобы оба перестали видеть сообщение целиком.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _redact_content(content: object) -> tuple[object, list[str]]:
    """Редактирование по месту, с сохранением формы content.

    Массив частей — стандартный формат OpenAI, его шлют все vision-клиенты
    (LibreChat, OpenWebUI, Cursor) и собирает сам веб-чат при любой
    прикреплённой картинке. Пока здесь была проверка isinstance(str),
    защита для этого формата была выключена целиком и молча: ключ уходил
    провайдеру, а в истории вызовов стояло «ничего не найдено».
    """
    if isinstance(content, str):
        return redact_text(content)
    if isinstance(content, list):
        found: list[str] = []
        parts = []
        for part in content:
            if isinstance(part, dict):
                new_part = dict(part)
                for key, value in part.items():
                    # Только строковые поля: image_url и прочие структуры
                    # проходят нетронутыми.
                    if isinstance(value, str):
                        new_value, part_found = redact_text(value)
                        new_part[key] = new_value
                        found.extend(part_found)
                parts.append(new_part)
            else:
                parts.append(part)
        return parts, found
    return content, []


def redact_messages(
    messages: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    all_found: list[str] = []
    redacted = []
    for m in messages:
        new_content, found = _redact_content(m.get("content"))
        all_found.extend(found)
        redacted.append({**m, "content": new_content} if "content" in m else dict(m))
    return redacted, all_found
