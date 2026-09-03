"""Идентично gateway/tests/test_pricing.py — pricing.py скопирован дословно
из AI-HUB/gateway, оба места ведутся синхронно."""

from decimal import Decimal

from app.models import ModelPrice
from app.pricing import UsageAmounts, compute_cost, select_best_price


def price(**kwargs) -> ModelPrice:
    defaults = dict(provider="test", model="m")
    defaults.update(kwargs)
    return ModelPrice(**defaults)


def test_per_image_cost():
    p = price(price_per_image=Decimal("0.08"))
    cost = compute_cost(p, UsageAmounts(images_count=2))
    assert cost == Decimal("0.160000")


def test_token_cost_gpt_image_like():
    p = price(
        price_per_1m_input_tokens=Decimal("5"),
        price_per_1m_input_image_tokens=Decimal("8"),
        price_per_1m_output_tokens=Decimal("30"),
    )
    usage = UsageAmounts(
        images_count=1,
        input_text_tokens=1000,
        input_image_tokens=2000,
        output_tokens=6000,
    )
    assert compute_cost(p, usage) == Decimal("0.201000")


def test_hybrid_price_sums_components():
    p = price(price_per_image=Decimal("0.10"), price_per_1m_input_tokens=Decimal("5"))
    usage = UsageAmounts(images_count=1, input_text_tokens=200_000)
    assert compute_cost(p, usage) == Decimal("1.100000")


def test_no_price_row():
    assert compute_cost(None, UsageAmounts(images_count=1)) is None


def test_no_usage_returns_none():
    p = price(price_per_image=Decimal("0.08"))
    assert compute_cost(p, UsageAmounts()) is None


def test_select_prefers_specific_quality():
    generic = price(id=1)
    low = price(id=2, quality="low")
    best = select_best_price([generic, low], quality="low", size=None)
    assert best.id == 2


def test_select_falls_back_to_generic():
    generic = price(id=1)
    low = price(id=2, quality="low")
    best = select_best_price([generic, low], quality="high", size=None)
    assert best.id == 1


def test_select_filters_size_mismatch():
    sized = price(id=1, size="1024x1024")
    assert select_best_price([sized], quality=None, size="1024x1536") is None


def test_select_quality_beats_size_on_tie():
    by_quality = price(id=1, quality="low")
    by_size = price(id=2, size="1024x1024")
    best = select_best_price([by_quality, by_size], quality="low", size="1024x1024")
    assert best.id == 1
