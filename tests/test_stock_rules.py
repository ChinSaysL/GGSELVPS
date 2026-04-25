from __future__ import annotations

from decimal import Decimal

from app.stock_rules import compute_usable_balance, evaluate_stock_status


def test_product_disabled_when_balance_too_low() -> None:
    usable_balance = compute_usable_balance(Decimal("18.00"), Decimal("0.15"))
    decision = evaluate_stock_status(
        mapping_enabled=True,
        supplier_is_available=True,
        supplier_stock=10,
        usable_balance_usd=usable_balance,
        supplier_cost_usd=Decimal("22.00"),
        has_positive_profit=True,
    )
    assert decision.in_stock is False
    assert decision.reason == "insufficient_usable_balance"


def test_product_enabled_when_balance_is_enough() -> None:
    usable_balance = compute_usable_balance(Decimal("400.00"), Decimal("0.15"))
    decision = evaluate_stock_status(
        mapping_enabled=True,
        supplier_is_available=True,
        supplier_stock=10,
        usable_balance_usd=usable_balance,
        supplier_cost_usd=Decimal("5.50"),
        has_positive_profit=True,
    )
    assert usable_balance == Decimal("340.0000")
    assert decision.in_stock is True
    assert decision.reason == "eligible"


def test_expensive_product_requires_extra_balance_buffer() -> None:
    decision = evaluate_stock_status(
        mapping_enabled=True,
        supplier_is_available=True,
        supplier_stock=10,
        usable_balance_usd=Decimal("340.00"),
        supplier_cost_usd=Decimal("350.00"),
        has_positive_profit=True,
    )
    assert decision.in_stock is False
    assert decision.required_balance_usd == Decimal("385.000")


def test_unavailable_supplier_product_is_disabled() -> None:
    decision = evaluate_stock_status(
        mapping_enabled=True,
        supplier_is_available=False,
        supplier_stock="300+",
        usable_balance_usd=Decimal("999.99"),
        supplier_cost_usd=Decimal("5.50"),
        has_positive_profit=True,
    )
    assert decision.in_stock is False
    assert decision.reason == "supplier_unavailable"


def test_disabled_mapping_keeps_product_out_of_stock() -> None:
    decision = evaluate_stock_status(
        mapping_enabled=False,
        supplier_is_available=True,
        supplier_stock="300+",
        usable_balance_usd=Decimal("999.99"),
        supplier_cost_usd=Decimal("5.50"),
        has_positive_profit=True,
    )
    assert decision.in_stock is False
    assert decision.reason == "mapping_disabled"
