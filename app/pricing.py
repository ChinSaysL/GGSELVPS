from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Literal


MONEY_QUANT = Decimal("0.01")
FOUR_DEC_QUANT = Decimal("0.0001")


class PricingConfigError(ValueError):
    pass


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise PricingConfigError(f"Invalid numeric value: {value}") from exc


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_UP)


def _quantize_four_dec(value: Decimal) -> Decimal:
    return value.quantize(FOUR_DEC_QUANT, rounding=ROUND_HALF_UP)


def get_markup_percent(supplier_cost_usd: Decimal | int | float | str) -> Decimal:
    cost = _to_decimal(supplier_cost_usd)
    if cost < Decimal("5"):
        return Decimal("0.30")
    if cost < Decimal("20"):
        return Decimal("0.22")
    if cost < Decimal("50"):
        return Decimal("0.15")
    return Decimal("0.10")


def get_min_profit_usd(supplier_cost_usd: Decimal | int | float | str) -> Decimal:
    cost = _to_decimal(supplier_cost_usd)
    if cost < Decimal("10"):
        return Decimal("0.50")
    if cost <= Decimal("30"):
        return Decimal("1.00")
    return Decimal("2.00")


@dataclass(frozen=True)
class PriceCalculation:
    supplier_cost_usd: Decimal
    final_price_usd: Decimal
    net_received_usd: Decimal
    profit_usd: Decimal
    markup_percent: Decimal
    min_profit_usd: Decimal


def calculate_final_price_usd(
    supplier_cost_usd: Decimal | int | float | str,
    *,
    pricing_mode: Literal["safe", "cost_plus_markup"] = "safe",
    platform_fee_percent: Decimal | int | float | str = Decimal("0.06"),
    fx_buffer_percent: Decimal | int | float | str = Decimal("0.03"),
    risk_buffer_percent: Decimal | int | float | str = Decimal("0.02"),
    markup_percent: Decimal | int | float | str | None = None,
    min_profit_usd: Decimal | int | float | str | None = None,
) -> PriceCalculation:
    cost = _to_decimal(supplier_cost_usd)
    if cost <= 0:
        raise PricingConfigError("supplier_cost_usd must be greater than 0.")

    effective_markup = (
        get_markup_percent(cost) if markup_percent is None else _to_decimal(markup_percent)
    )
    if effective_markup < 0:
        raise PricingConfigError("markup_percent must be >= 0.")

    if pricing_mode == "cost_plus_markup":
        effective_min_profit = (
            Decimal("0") if min_profit_usd is None else _to_decimal(min_profit_usd)
        )
        if effective_min_profit < 0:
            raise PricingConfigError("min_profit_usd must be >= 0.")

        final_price = cost * (Decimal("1") + effective_markup)
        final_price = _quantize_money(final_price)
        net_received = final_price
        profit = net_received - cost

        if profit < effective_min_profit:
            final_price = _quantize_money(cost + effective_min_profit)
            net_received = final_price
            profit = net_received - cost

        return PriceCalculation(
            supplier_cost_usd=_quantize_four_dec(cost),
            final_price_usd=_quantize_money(final_price),
            net_received_usd=_quantize_four_dec(net_received),
            profit_usd=_quantize_four_dec(profit),
            markup_percent=_quantize_four_dec(effective_markup),
            min_profit_usd=_quantize_money(effective_min_profit),
        )

    if pricing_mode != "safe":
        raise PricingConfigError(f"Unsupported pricing_mode: {pricing_mode}")

    platform_fee = _to_decimal(platform_fee_percent)
    fx_buffer = _to_decimal(fx_buffer_percent)
    risk_buffer = _to_decimal(risk_buffer_percent)

    if not Decimal("0") <= platform_fee < Decimal("1"):
        raise PricingConfigError("platform_fee_percent must be within [0, 1).")
    if fx_buffer < 0 or risk_buffer < 0:
        raise PricingConfigError("fx_buffer_percent and risk_buffer_percent must be >= 0.")

    effective_min_profit = (
        get_min_profit_usd(cost) if min_profit_usd is None else _to_decimal(min_profit_usd)
    )
    if effective_min_profit < 0:
        raise PricingConfigError("min_profit_usd must be >= 0.")

    base_price = cost
    base_price *= Decimal("1") + fx_buffer
    base_price *= Decimal("1") + risk_buffer
    base_price *= Decimal("1") + effective_markup

    final_price = base_price / (Decimal("1") - platform_fee)
    final_price = _quantize_money(final_price)

    net_received = final_price * (Decimal("1") - platform_fee)
    profit = net_received - cost

    if profit < effective_min_profit:
        final_price = (cost + effective_min_profit) / (Decimal("1") - platform_fee)
        final_price = _quantize_money(final_price)
        net_received = final_price * (Decimal("1") - platform_fee)
        profit = net_received - cost

    return PriceCalculation(
        supplier_cost_usd=_quantize_four_dec(cost),
        final_price_usd=_quantize_money(final_price),
        net_received_usd=_quantize_four_dec(net_received),
        profit_usd=_quantize_four_dec(profit),
        markup_percent=_quantize_four_dec(effective_markup),
        min_profit_usd=_quantize_money(effective_min_profit),
    )


def calculate_compounded_price(
    base_amount: Decimal | int | float | str,
    *,
    markup_percent: Decimal | int | float | str,
    fee_percent: Decimal | int | float | str,
    fx_buffer_percent: Decimal | int | float | str = Decimal("0"),
) -> Decimal:
    base = _to_decimal(base_amount)
    markup = _to_decimal(markup_percent)
    fee = _to_decimal(fee_percent)
    fx_buffer = _to_decimal(fx_buffer_percent)

    if base <= 0:
        raise PricingConfigError("base_amount must be greater than 0.")
    if markup < 0 or fee < 0 or fx_buffer < 0:
        raise PricingConfigError("markup_percent, fee_percent, and fx_buffer_percent must be >= 0.")

    compounded = base
    compounded *= Decimal("1") + markup
    compounded *= Decimal("1") + fee
    compounded *= Decimal("1") + fx_buffer
    return compounded.quantize(MONEY_QUANT, rounding=ROUND_DOWN)


def convert_usd_to_currency(
    amount_usd: Decimal | int | float | str,
    target_currency: str,
    manual_usd_rub_rate: Decimal | int | float | str | None = None,
) -> Decimal:
    amount = _to_decimal(amount_usd)
    currency = target_currency.upper()

    if currency == "USD":
        return _quantize_money(amount)

    if currency == "RUB":
        if manual_usd_rub_rate is None:
            raise PricingConfigError(
                "MANUAL_USD_RUB_RATE is required when target currency is RUB."
            )

        rate = _to_decimal(manual_usd_rub_rate)
        if rate <= 0:
            raise PricingConfigError("MANUAL_USD_RUB_RATE must be greater than 0.")
        return _quantize_money(amount * rate)

    raise PricingConfigError(f"Unsupported target currency: {target_currency}")
