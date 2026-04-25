from __future__ import annotations

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


def run_stock_sync() -> dict[str, int | str]:
    log_event(
        logger,
        "stock_sync.skipped",
        reason="api_clients_not_implemented",
        offers_enabled=0,
        offers_disabled=0,
    )
    return {
        "status": "skipped",
        "reason": "API client endpoints are not implemented yet.",
        "offers_enabled": 0,
        "offers_disabled": 0,
    }
