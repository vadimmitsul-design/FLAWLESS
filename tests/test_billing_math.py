from decimal import Decimal

from app.billing import price_in_rub
from app.models import PricingConfig


def test_price_in_rub_applies_markup_and_fx():
    cfg = PricingConfig(markup_percent=Decimal("30.00"), usd_rub_rate=Decimal("100.0000"))
    # 1 USD себестоимости * 1.30 наценки * 100 руб/$ = 130 руб
    assert price_in_rub(Decimal("1.000000"), cfg) == Decimal("130.0000")


def test_price_in_rub_zero_markup():
    cfg = PricingConfig(markup_percent=Decimal("0.00"), usd_rub_rate=Decimal("90.0000"))
    assert price_in_rub(Decimal("2.000000"), cfg) == Decimal("180.0000")


def test_price_in_rub_rounds_to_four_decimals():
    cfg = PricingConfig(markup_percent=Decimal("30.00"), usd_rub_rate=Decimal("95.0000"))
    # 0.0000225 * 1.3 * 95 = 0.00277875 -> округление до 0.0028
    result = price_in_rub(Decimal("0.0000225"), cfg)
    assert result == Decimal("0.0028")
