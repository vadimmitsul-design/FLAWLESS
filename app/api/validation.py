"""api / validation for the Flawless application."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException


def money_field(raw: str, field: str, *, allow_negative: bool = False) -> Decimal | None:
    """Денежное поле формы -> Decimal или None, если поле пустое.

    Один разбор на все формы: раньше значение уходило прямо в Decimal(), и
    любой ввод, которого Decimal не понимает — русская запятая «1,5», пробел
    между тысячами, опечатка — давал 500 на денежной форме вместо внятной
    ошибки. Отрицательное значение при этом принималось молча и блокировало
    ключ: `0 >= -5` истинно всегда.
    """
    cleaned = (raw or "").strip().replace(",", ".").replace("\u00a0", "").replace(" ", "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"{field}: не похоже на сумму — «{raw}»"
        ) from exc
    if not value.is_finite():
        raise HTTPException(status_code=400, detail=f"{field}: сумма должна быть конечной")
    if not allow_negative and value < 0:
        raise HTTPException(status_code=400, detail=f"{field}: сумма не может быть отрицательной")
    return value


def parse_date(raw: str | None) -> datetime | None:
    """Дата из формы (YYYY-MM-DD) в UTC-полночь. Пустое поле — это None, а не
    ошибка: срок может быть неизвестен."""
    if not raw or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"не похоже на дату: {raw}") from exc
