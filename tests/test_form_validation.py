"""Monetary form inputs must never introduce non-finite Decimal values."""

import pytest
from fastapi import HTTPException

from app.api.validation import money_field


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_money_is_rejected(raw: str) -> None:
    with pytest.raises(HTTPException) as error:
        money_field(raw, "amount")
    assert error.value.status_code == 400
