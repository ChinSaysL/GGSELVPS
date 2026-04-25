from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.services.order_sync import run_order_sync
from app.services.price_sync import run_price_sync
from app.services.product_sync import run_product_sync
from app.services.stock_sync import run_stock_sync
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)
_scheduler: BackgroundScheduler | None = None


def start_scheduler(settings: Settings) -> None:
    global _scheduler

    if _scheduler is not None:
        return

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_product_sync,
        trigger="interval",
        minutes=settings.product_sync_interval_minutes,
        id="product_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        run_price_sync,
        trigger="interval",
        minutes=settings.price_sync_interval_minutes,
        id="price_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        run_stock_sync,
        trigger="interval",
        minutes=settings.stock_sync_interval_minutes,
        id="stock_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        run_order_sync,
        trigger="interval",
        seconds=settings.order_poll_interval_seconds,
        id="order_sync",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log_event(logger, "scheduler.started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    log_event(logger, "scheduler.stopped")
