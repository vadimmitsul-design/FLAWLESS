"""Грубый DLP-фильтр: вырезает похожее на секреты из промпта ПЕРЕД отправкой
провайдеру. Хранится только СПИСОК СРАБОТАВШИХ ТИПОВ (для видимости в
истории вызовов), не сам секрет — иначе БД сама стала бы хранилищем
чувствительных данных, которые мы же и пытаемся защитить.

Паттерны специально консервативные (требуют контекста типа "ИНН:"/"пароль:")
там, где иначе была бы лавина ложных срабатываний на обычные числа/код.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
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
        def _repl(m, _name=name):
            found.append(_name)
            return f"[REDACTED:{_name}]"

        text = pattern.sub(_repl, text)
    return text, found


def redact_messages(messages: list[dict]) -> tuple[list[dict], list[str]]:
    all_found: list[str] = []
    redacted = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            new_content, found = redact_text(content)
            all_found.extend(found)
            redacted.append({**m, "content": new_content})
        else:
            redacted.append(m)
    return redacted, all_found
