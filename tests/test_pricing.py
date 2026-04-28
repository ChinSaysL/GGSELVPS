from __future__ import annotations

from decimal import Decimal

import pytest

from app.pricing import (
    PricingConfigError,
    calculate_compounded_price,
    calculate_final_price_usd,
    convert_usd_to_currency,
    get_markup_percent,
    get_min_profit_usd,
)


@pytest.mark.parametrize(
    ("cost", "expected_markup"),
    [
        (Decimal("4.99"), Decimal("0.30")),
        (Decimal("5.00"), Decimal("0.22")),
        (Decimal("19.99"), Decimal("0.22")),
        (Decimal("20.00"), Decimal("0.15")),
        (Decimal("49.99"), Decimal("0.15")),
        (Decimal("50.00"), Decimal("0.10")),
    ],
)
def test_markup_tiers(cost: Decimal, expected_markup: Decimal) -> None:
    assert get_markup_percent(cost) == expected_markup


@pytest.mark.parametrize(
    ("cost", "expected_min_profit"),
    [
        (Decimal("9.99"), Decimal("0.50")),
        (Decimal("10.00"), Decimal("1.00")),
        (Decimal("30.00"), Decimal("1.00")),
        (Decimal("30.01"), Decimal("2.00")),
    ],
)
def test_min_profit_tiers(cost: Decimal, expected_min_profit: Decimal) -> None:
    assert get_min_profit_usd(cost) == expected_min_profit


def test_platform_fee_fx_and_risk_buffers_are_applied() -> None:
    with_buffers = calculate_final_price_usd(
        Decimal("5.50"),
        platform_fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.03"),
        risk_buffer_percent=Decimal("0.02"),
        markup_percent=Decimal("0.22"),
    )
    without_buffers = calculate_final_price_usd(
        Decimal("5.50"),
        platform_fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.00"),
        risk_buffer_percent=Decimal("0.00"),
        markup_percent=Decimal("0.22"),
    )

    assert with_buffers.final_price_usd > without_buffers.final_price_usd
    assert with_buffers.profit_usd > Decimal("0")


def test_cost_plus_markup_mode_uses_direct_cost_markup_formula() -> None:
    calculation = calculate_final_price_usd(
        Decimal("5.50"),
        pricing_mode="cost_plus_markup",
        platform_fee_percent=Decimal("0.50"),
        fx_buffer_percent=Decimal("0.99"),
        risk_buffer_percent=Decimal("0.99"),
        markup_percent=Decimal("0.20"),
    )

    assert calculation.final_price_usd == Decimal("6.60")
    assert calculation.profit_usd == Decimal("1.1000")


def test_cost_plus_markup_mode_allows_optional_min_profit_floor() -> None:
    calculation = calculate_final_price_usd(
        Decimal("5.50"),
        pricing_mode="cost_plus_markup",
        markup_percent=Decimal("0.02"),
        min_profit_usd=Decimal("1.00"),
    )

    assert calculation.final_price_usd == Decimal("6.50")
    assert calculation.profit_usd == Decimal("1.0000")


def test_min_profit_is_enforced() -> None:
    calculation = calculate_final_price_usd(
        Decimal("109.99"),
        platform_fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.00"),
        risk_buffer_percent=Decimal("0.00"),
        markup_percent=Decimal("0.00"),
        min_profit_usd=Decimal("2.00"),
    )

    assert calculation.profit_usd >= Decimal("2.00")
    assert calculation.final_price_usd > Decimal("109.99")


def test_example_prices_are_positive_without_competitor_data() -> None:
    cheap = calculate_final_price_usd(
        Decimal("5.50"),
        platform_fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.03"),
        risk_buffer_percent=Decimal("0.02"),
        markup_percent=Decimal("0.22"),
    )
    expensive = calculate_final_price_usd(
        Decimal("109.99"),
        platform_fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.03"),
        risk_buffer_percent=Decimal("0.02"),
        markup_percent=Decimal("0.10"),
    )

    assert cheap.profit_usd >= cheap.min_profit_usd
    assert expensive.profit_usd >= Decimal("2.00")


def test_usd_to_rub_requires_manual_rate() -> None:
    with pytest.raises(PricingConfigError, match="MANUAL_USD_RUB_RATE"):
        convert_usd_to_currency(Decimal("10.00"), "RUB", None)


def test_usd_to_rub_conversion_with_manual_rate() -> None:
    rub_price = convert_usd_to_currency(Decimal("10.00"), "RUB", Decimal("95.0"))
    assert rub_price == Decimal("950.00")


def test_calculate_compounded_price_uses_compound_multipliers_and_rounds_down() -> None:
    price = calculate_compounded_price(
        Decimal("1.00"),
        markup_percent=Decimal("0.15"),
        fee_percent=Decimal("0.06"),
        fx_buffer_percent=Decimal("0.03"),
    )
    assert price == Decimal("1.25")
