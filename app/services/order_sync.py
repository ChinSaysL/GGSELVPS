from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from app.models import Order
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

OrderStatus = Literal[
    "pending",
    "processing",
    "supplier_order_created",
    "delivered",
    "failed",
    "needs_manual_review",
]

RetryAction = Literal["none", "retry_processing", "retry_delivery"]


def should_create_supplier_purchase(existing_status: str | None) -> bool:
    if existing_status is None:
        return True
    return existing_status == "pending"


def should_skip_order(existing_status: str | None) -> bool:
    return existing_status in {"processing", "supplier_order_created", "delivered"}


def get_retry_action(status: str, supplier_order_id: str | None) -> RetryAction:
    if status not in {"failed", "needs_manual_review"}:
        return "none"
    if supplier_order_id:
        return "retry_delivery"
    return "retry_processing"


def run_order_sync() -> dict[str, int | str]:
    log_event(
        logger,
        "order_sync.skipped",
        reason="api_clients_not_implemented",
        orders_detected=0,
        orders_processed=0,
    )
    return {
        "status": "skipped",
        "reason": "API client endpoints are not implemented yet.",
        "orders_detected": 0,
        "orders_processed": 0,
    }


def retry_order(order: Order, db: Session) -> dict[str, str]:
    action = get_retry_action(order.status, order.supplier_order_id)
    if action == "none":
        return {
            "status": "skipped",
            "reason": "Order is not in a retryable state.",
        }

    if action == "retry_delivery":
        order.status = "supplier_order_created"
        order.error_message = None
        db.add(order)
        db.commit()
        return {
            "status": "queued",
            "action": "retry_delivery",
        }

    order.status = "pending"
    order.error_message = None
    db.add(order)
    db.commit()
    return {
        "status": "queued",
        "action": "retry_processing",
    }
