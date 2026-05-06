from __future__ import annotations

from app.services.order_sync import (
    get_retry_action,
    should_create_supplier_purchase,
    should_skip_order,
)


def test_same_order_processed_once_when_already_in_progress() -> None:
    assert should_skip_order("processing") is True
    assert should_skip_order("supplier_order_created") is True


def test_existing_delivered_order_not_purchased_again() -> None:
    assert should_skip_order("delivered") is True
    assert should_create_supplier_purchase("delivered") is False


def test_existing_cancelled_order_not_purchased_again() -> None:
    assert should_skip_order("cancelled") is True
    assert should_skip_order("canceled") is True
    assert should_create_supplier_purchase("cancelled") is False


def test_supplier_success_delivery_failure_retries_delivery_only() -> None:
    assert get_retry_action("failed", supplier_order_id="sup-123") == "retry_delivery"


def test_restart_safe_state_handling_uses_persisted_status() -> None:
    assert should_create_supplier_purchase("pending") is True
    assert should_create_supplier_purchase("processing") is False
    assert should_create_supplier_purchase("supplier_order_created") is False
