from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


EXPENSIVE_PRODUCT_THRESHOLD_USD = Decimal("50")
EXPENSIVE_PRODUCT_BALANCE_MULTIPLIER = Decimal("1.10")

UNLIMITED_STOCK_MARKERS = {
    "unlimited",
    "300+",
    "infinite",
    "∞",
}


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value: {value}") from exc


def has_supplier_stock(supplier_stock: int | float | Decimal | str | None) -> bool:
    if supplier_stock is None:
        return False

    if isinstance(supplier_stock, (int, float, Decimal)):
        return _to_decimal(supplier_stock) > 0

    stock_text = str(supplier_stock).strip().lower()
    if not stock_text:
        return False

    if stock_text in UNLIMITED_STOCK_MARKERS:
        return True

    if stock_text.endswith("+"):
        numeric_text = stock_text.removesuffix("+").strip()
        if numeric_text.isdigit():
            return int(numeric_text) > 0

    try:
        return Decimal(stock_text) > 0
    except InvalidOperation:
        return False


def compute_usable_balance(
    gengine_balance_usd: Decimal | int | float | str,
    safety_balance_percent: Decimal | int | float | str = Decimal("0.15"),
) -> Decimal:
    balance = _to_decimal(gengine_balance_usd)
    safety = _to_decimal(safety_balance_percent)
    if not Decimal("0") <= safety < Decimal("1"):
        raise ValueError("safety_balance_percent must be within [0, 1).")
    return balance * (Decimal("1") - safety)


def required_balance_for_product(
    supplier_cost_usd: Decimal | int | float | str,
) -> Decimal:
    cost = _to_decimal(supplier_cost_usd)
    if cost >= EXPENSIVE_PRODUCT_THRESHOLD_USD:
        return cost * EXPENSIVE_PRODUCT_BALANCE_MULTIPLIER
    return cost


@dataclass(frozen=True)
class StockDecision:
    in_stock: bool
    reason: str
    required_balance_usd: Decimal


def evaluate_stock_status(
    *,
    mapping_enabled: bool,
    supplier_is_available: bool,
    supplier_stock: int | float | Decimal | str | None,
    usable_balance_usd: Decimal | int | float | str,
    supplier_cost_usd: Decimal | int | float | str,
    has_positive_profit: bool,
    manually_disabled: bool = False,
) -> StockDecision:
    required_balance = required_balance_for_product(supplier_cost_usd)
    usable_balance = _to_decimal(usable_balance_usd)

    if not mapping_enabled:
        return StockDecision(
            in_stock=False,
            reason="mapping_disabled",
            required_balance_usd=required_balance,
        )
    if manually_disabled:
        return StockDecision(
            in_stock=False,
            reason="manually_disabled",
            required_balance_usd=required_balance,
        )
    if not supplier_is_available:
        return StockDecision(
            in_stock=False,
            reason="supplier_unavailable",
            required_balance_usd=required_balance,
        )
    if not has_supplier_stock(supplier_stock):
        return StockDecision(
            in_stock=False,
            reason="supplier_no_stock",
            required_balance_usd=required_balance,
        )
    if not has_positive_profit:
        return StockDecision(
            in_stock=False,
            reason="non_positive_profit",
            required_balance_usd=required_balance,
        )
    if usable_balance < required_balance:
        return StockDecision(
            in_stock=False,
            reason="insufficient_usable_balance",
            required_balance_usd=required_balance,
        )

    return StockDecision(
        in_stock=True,
        reason="eligible",
        required_balance_usd=required_balance,
    )
