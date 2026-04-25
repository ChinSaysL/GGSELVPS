from __future__ import annotations

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


def run_price_sync() -> dict[str, int | str]:
    log_event(
        logger,
        "price_sync.skipped",
        reason="api_clients_not_implemented",
        prices_updated=0,
    )
    return {
        "status": "skipped",
        "reason": "API client endpoints are not implemented yet.",
        "prices_updated": 0,
    }
