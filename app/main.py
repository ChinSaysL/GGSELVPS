from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, init_db
from app.models import BalanceSnapshot, MarketplaceOffer, Order, ProductMapping, SupplierProduct
from app.services.order_sync import retry_order, run_order_sync
from app.services.price_sync import run_price_sync
from app.services.product_sync import run_product_sync
from app.services.stock_sync import run_stock_sync
from app.utils.logging import configure_logging, get_logger, log_event
from app.workers.scheduler import start_scheduler, stop_scheduler


settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)

app = FastAPI(
    title="Gift Card Middleware",
    version="0.1.0",
    description="VPS middleware for gift-card resale automation.",
)


class MappingImportItem(BaseModel):
    supplier_product_id: int
    marketplace_offer_id: int
    enabled: bool = True
    min_margin_percent: Decimal | None = None
    min_profit_usd: Decimal | None = None
    max_price: Decimal | None = None


class MappingImportRequest(BaseModel):
    items: list[MappingImportItem] = Field(default_factory=list)


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    if settings.scheduler_enabled:
        start_scheduler(settings)
    log_event(logger, "app.started", scheduler_enabled=settings.scheduler_enabled)


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_scheduler()
    log_event(logger, "app.stopped")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/balance")
def get_latest_balance(db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(BalanceSnapshot).order_by(
        desc(BalanceSnapshot.created_at), desc(BalanceSnapshot.id)
    )
    latest = db.execute(stmt).scalars().first()
    if latest is None:
        return {"status": "empty", "message": "No balance snapshots yet."}

    return {
        "status": "ok",
        "supplier": latest.supplier,
        "balance_usd": _serialize_decimal(latest.balance_usd),
        "usable_balance_usd": _serialize_decimal(latest.usable_balance_usd),
        "created_at": _serialize_datetime(latest.created_at),
    }


@app.get("/products")
def list_products(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(SupplierProduct).order_by(desc(SupplierProduct.updated_at)).limit(limit)
    products = db.execute(stmt).scalars().all()
    return [
        {
            "id": product.id,
            "supplier": product.supplier,
            "supplier_product_id": product.supplier_product_id,
            "title": product.title,
            "category": product.category,
            "denomination": product.denomination,
            "region": product.region,
            "cost_usd": _serialize_decimal(product.cost_usd),
            "supplier_stock": product.supplier_stock,
            "is_available": product.is_available,
            "updated_at": _serialize_datetime(product.updated_at),
        }
        for product in products
    ]


@app.get("/offers")
def list_offers(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(MarketplaceOffer).order_by(desc(MarketplaceOffer.updated_at)).limit(limit)
    offers = db.execute(stmt).scalars().all()
    response: list[dict[str, Any]] = []
    for offer in offers:
        mapping = next(iter(offer.mappings), None)
        response.append(
            {
                "id": offer.id,
                "marketplace": offer.marketplace,
                "marketplace_offer_id": offer.marketplace_offer_id,
                "title": offer.title,
                "price": _serialize_decimal(offer.price),
                "currency": offer.currency,
                "is_enabled": offer.is_enabled,
                "supplier_product_id": offer.supplier_product_id,
                "supplier_product_title": (
                    offer.supplier_product.title if offer.supplier_product else None
                ),
                "mapping_id": mapping.id if mapping else None,
                "mapping_enabled": mapping.enabled if mapping else None,
                "last_synced_at": _serialize_datetime(offer.last_synced_at),
            }
        )
    return response


@app.post("/sync/products")
def sync_products() -> dict[str, Any]:
    return run_product_sync()


@app.post("/sync/prices")
def sync_prices() -> dict[str, int | str]:
    return run_price_sync()


@app.post("/sync/stock")
def sync_stock() -> dict[str, int | str]:
    return run_stock_sync()


@app.get("/orders")
def list_orders(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Order).order_by(desc(Order.updated_at)).limit(limit)
    orders = db.execute(stmt).scalars().all()
    return [
        {
            "id": order.id,
            "marketplace": order.marketplace,
            "marketplace_order_id": order.marketplace_order_id,
            "marketplace_offer_id": order.marketplace_offer_id,
            "supplier_product_id": order.supplier_product_id,
            "buyer_id": order.buyer_id,
            "status": order.status,
            "supplier_order_id": order.supplier_order_id,
            "cost_usd": _serialize_decimal(order.cost_usd),
            "sale_price": _serialize_decimal(order.sale_price),
            "error_message": order.error_message,
            "created_at": _serialize_datetime(order.created_at),
            "updated_at": _serialize_datetime(order.updated_at),
        }
        for order in orders
    ]


@app.post("/orders/{order_id}/retry")
def retry_order_endpoint(order_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return retry_order(order, db)


@app.post("/mappings/import")
def import_mappings(
    payload: MappingImportRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=400, detail="No mapping items provided.")

    created = 0
    updated = 0
    skipped: list[dict[str, Any]] = []

    for item in payload.items:
        supplier = db.get(SupplierProduct, item.supplier_product_id)
        offer = db.get(MarketplaceOffer, item.marketplace_offer_id)
        if supplier is None or offer is None:
            skipped.append(
                {
                    "supplier_product_id": item.supplier_product_id,
                    "marketplace_offer_id": item.marketplace_offer_id,
                    "reason": "supplier_or_offer_missing",
                }
            )
            continue

        stmt = select(ProductMapping).where(
            ProductMapping.supplier_product_id == item.supplier_product_id,
            ProductMapping.marketplace_offer_id == item.marketplace_offer_id,
        )
        existing = db.execute(stmt).scalars().first()
        if existing is None:
            mapping = ProductMapping(
                supplier_product_id=item.supplier_product_id,
                marketplace_offer_id=item.marketplace_offer_id,
                enabled=item.enabled,
                min_margin_percent=item.min_margin_percent,
                min_profit_usd=item.min_profit_usd,
                max_price=item.max_price,
            )
            db.add(mapping)
            created += 1
            continue

        existing.enabled = item.enabled
        existing.min_margin_percent = item.min_margin_percent
        existing.min_profit_usd = item.min_profit_usd
        existing.max_price = item.max_price
        db.add(existing)
        updated += 1

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


def _set_mapping_enabled(mapping_id: int, enabled: bool, db: Session) -> dict[str, Any]:
    mapping = db.get(ProductMapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    mapping.enabled = enabled
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return {
        "status": "ok",
        "mapping_id": mapping.id,
        "enabled": mapping.enabled,
    }


@app.post("/mappings/{mapping_id}/enable")
def enable_mapping(mapping_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_mapping_enabled(mapping_id, True, db)


@app.post("/mappings/{mapping_id}/disable")
def disable_mapping(mapping_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_mapping_enabled(mapping_id, False, db)


@app.post("/sync/orders")
def sync_orders() -> dict[str, int | str]:
    return run_order_sync()
