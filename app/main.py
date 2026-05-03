from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.clients import DigisellerClient, GEngineClient, GGSELClient
from app.config import get_settings
from app.db import get_db, init_db
from app.models import BalanceSnapshot, MarketplaceOffer, Order, ProductMapping, SupplierProduct
from app.services.order_sync import process_digiseller_unique_code, retry_order, run_order_sync
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
    marketplace_commission_percent: Decimal | None = None
    payment_system_commission_percent: Decimal | None = None
    min_margin_percent: Decimal | None = None
    min_profit_usd: Decimal | None = None
    max_price: Decimal | None = None
    manage_stock_quantity: bool = False
    min_purchase_quantity: int | None = None


class MappingImportRequest(BaseModel):
    items: list[MappingImportItem] = Field(default_factory=list)


class ManualOfferLinkRequest(BaseModel):
    marketplace: Literal["digiseller", "ggsel"]
    marketplace_offer_id: str
    supplier_product_id: int
    title: str
    price: Decimal
    currency: str = "RUB"
    offer_enabled: bool = False
    manually_disabled: bool | None = None
    requires_buyer_input: bool = False
    buyer_input_label: str | None = None
    buyer_input_help: str | None = None
    mapping_enabled: bool = True
    marketplace_variant_id: int | None = None
    marketplace_variant_name: str | None = None
    marketplace_commission_percent: Decimal | None = None
    payment_system_commission_percent: Decimal | None = None
    min_margin_percent: Decimal | None = None
    min_profit_usd: Decimal | None = None
    max_price: Decimal | None = None
    manage_stock_quantity: bool = False
    min_purchase_quantity: int | None = None


class RechargeVariantSpec(BaseModel):
    variant_id: int
    variant_name: str


class AutoLinkRechargeDenominationsRequest(BaseModel):
    marketplace: Literal["digiseller", "ggsel"]
    marketplace_offer_id: str
    supplier_service_id: str  # G-Engine service ID (e.g., "5")
    variants: list[RechargeVariantSpec]
    marketplace_commission_percent: Decimal | None = None
    payment_system_commission_percent: Decimal | None = None
    manage_stock_quantity: bool = False
    min_purchase_quantity: int | None = None
    dry_run: bool = False


def _parse_additive_quantity(value: str | None) -> Decimal | None:
    """Extract total quantity, summing additive notation: '32 + 3 Diamonds' → Decimal('35')."""
    text = str(value or "").strip()
    if not text:
        return None
    compact = re.sub(r"[\s  ]+", " ", text).strip()
    if "+" in compact:
        chain_match = re.match(r"^(\d+(?:[.,]\d+)?)(?:\s*\+\s*\d+(?:[.,]\d+)?)+", compact)
        if chain_match:
            nums = re.findall(r"\d+(?:[.,]\d+)?", chain_match.group(0))
            try:
                total = sum(Decimal(n.replace(",", ".")) for n in nums)
                if total > 0:
                    return total
            except (InvalidOperation, ValueError):
                pass
    match = re.search(r"\d+(?:[.,]\d+)?", compact)
    if match is None:
        return None
    try:
        parsed = Decimal(match.group(0).replace(",", "."))
        return parsed if parsed > 0 else None
    except (InvalidOperation, ValueError):
        return None


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
                "manually_disabled": offer.manually_disabled,
                "requires_buyer_input": offer.requires_buyer_input,
                "buyer_input_label": offer.buyer_input_label,
                "buyer_input_help": offer.buyer_input_help,
                "supplier_product_id": offer.supplier_product_id,
                "supplier_product_title": (
                    offer.supplier_product.title if offer.supplier_product else None
                ),
                "mapping_id": mapping.id if mapping else None,
                "mapping_enabled": mapping.enabled if mapping else None,
                "mapping_variant_id": mapping.marketplace_variant_id if mapping else None,
                "mapping_variant_name": mapping.marketplace_variant_name if mapping else None,
                "mapping_marketplace_commission_percent": (
                    _serialize_decimal(mapping.marketplace_commission_percent) if mapping else None
                ),
                "mapping_payment_system_commission_percent": (
                    _serialize_decimal(mapping.payment_system_commission_percent) if mapping else None
                ),
                "last_synced_at": _serialize_datetime(offer.last_synced_at),
            }
        )
    return response


@app.post("/sync/products")
def sync_products() -> dict[str, Any]:
    return run_product_sync()


@app.post("/sync/prices")
def sync_prices(dry_run: bool = Query(default=False)) -> dict[str, Any]:
    return run_price_sync(dry_run=dry_run)


@app.post("/sync/stock")
def sync_stock() -> dict[str, Any]:
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
                marketplace_commission_percent=item.marketplace_commission_percent,
                payment_system_commission_percent=item.payment_system_commission_percent,
                min_margin_percent=item.min_margin_percent,
                min_profit_usd=item.min_profit_usd,
                max_price=item.max_price,
                manage_stock_quantity=item.manage_stock_quantity,
                min_purchase_quantity=item.min_purchase_quantity,
            )
            db.add(mapping)
            created += 1
            continue

        existing.enabled = item.enabled
        existing.marketplace_commission_percent = item.marketplace_commission_percent
        existing.payment_system_commission_percent = item.payment_system_commission_percent
        existing.min_margin_percent = item.min_margin_percent
        existing.min_profit_usd = item.min_profit_usd
        existing.max_price = item.max_price
        existing.manage_stock_quantity = item.manage_stock_quantity
        existing.min_purchase_quantity = item.min_purchase_quantity
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


def _set_offer_manual_halt(offer_id: int, manually_disabled: bool, db: Session) -> dict[str, Any]:
    offer = db.get(MarketplaceOffer, offer_id)
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    offer.manually_disabled = manually_disabled
    if manually_disabled:
        offer.is_enabled = False
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return {
        "status": "ok",
        "offer_id": offer.id,
        "marketplace": offer.marketplace,
        "marketplace_offer_id": offer.marketplace_offer_id,
        "manually_disabled": offer.manually_disabled,
        "is_enabled": offer.is_enabled,
    }


@app.post("/offers/{offer_id}/halt")
def halt_offer(offer_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_offer_manual_halt(offer_id, True, db)


@app.post("/offers/{offer_id}/resume")
def resume_offer(offer_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_offer_manual_halt(offer_id, False, db)


@app.post("/sync/orders")
def sync_orders() -> dict[str, Any]:
    return run_order_sync()


def _digiseller_delivery_response(stats: dict[str, Any]) -> HTMLResponse:
    status = str(stats.get("status") or "")
    if status in {"ok", "success"} and int(stats.get("orders_delivered") or 0) > 0:
        return HTMLResponse(
            "<html><body><h1>Order delivered</h1>"
            "<p>Your order has been delivered. You can close this page.</p></body></html>"
        )
    if status == "pending":
        return HTMLResponse(
            "<html><body><h1>Order is being prepared</h1>"
            "<p>Your payment was verified, but supplier delivery is not ready yet. "
            "Please refresh this page in a few minutes.</p></body></html>",
            status_code=202,
        )
    return HTMLResponse(
        "<html><body><h1>Delivery needs support</h1>"
        "<p>We could not complete automatic delivery. Please contact the seller in chat.</p>"
        "</body></html>",
        status_code=500,
    )


@app.get("/delivery/digiseller")
def digiseller_unique_code_delivery(
    unique_code: str = Query(..., alias="uniquecode"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not settings.gengine_api_key or not settings.digiseller_api_key:
        raise HTTPException(status_code=503, detail="Delivery API keys are not configured.")

    supplier_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
    marketplace_client = DigisellerClient(
        settings.digiseller_base_url,
        settings.digiseller_api_key,
        seller_id=settings.digiseller_seller_id,
        task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
        task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
        rate_limit_per_minute=settings.digiseller_rate_limit_per_minute,
    )
    try:
        stats = process_digiseller_unique_code(
            unique_code=unique_code,
            db=db,
            settings=settings,
            supplier_client=supplier_client,
            marketplace_client=marketplace_client,
        )
        return _digiseller_delivery_response(stats)
    finally:
        marketplace_client.close()
        supplier_client.close()


@app.post("/offers/manual-link")
def manual_link_offer(
    payload: ManualOfferLinkRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    supplier = db.get(SupplierProduct, payload.supplier_product_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="Supplier product not found.")

    offer_stmt = select(MarketplaceOffer).where(
        MarketplaceOffer.marketplace == payload.marketplace,
        MarketplaceOffer.marketplace_offer_id == payload.marketplace_offer_id,
    )
    offer = db.execute(offer_stmt).scalars().first()
    offer_created = False

    if offer is None:
        manually_disabled = bool(payload.manually_disabled)
        offer = MarketplaceOffer(
            marketplace=payload.marketplace,
            marketplace_offer_id=payload.marketplace_offer_id,
            supplier_product_id=payload.supplier_product_id,
            title=payload.title,
            price=payload.price,
            currency=payload.currency.upper(),
            is_enabled=payload.offer_enabled and not manually_disabled,
            manually_disabled=manually_disabled,
            requires_buyer_input=payload.requires_buyer_input,
            buyer_input_label=payload.buyer_input_label,
            buyer_input_help=payload.buyer_input_help,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(offer)
        db.flush()
        offer_created = True
    else:
        offer.supplier_product_id = payload.supplier_product_id
        offer.title = payload.title
        offer.price = payload.price
        offer.currency = payload.currency.upper()
        offer.is_enabled = payload.offer_enabled
        if payload.manually_disabled is not None:
            offer.manually_disabled = payload.manually_disabled
        if offer.manually_disabled:
            offer.is_enabled = False
        offer.requires_buyer_input = payload.requires_buyer_input
        offer.buyer_input_label = payload.buyer_input_label
        offer.buyer_input_help = payload.buyer_input_help
        offer.last_synced_at = datetime.now(timezone.utc)
        db.add(offer)
        db.flush()

    mapping_stmt = select(ProductMapping).where(
        ProductMapping.supplier_product_id == payload.supplier_product_id,
        ProductMapping.marketplace_offer_id == offer.id,
    )
    mapping = db.execute(mapping_stmt).scalars().first()
    mapping_created = False

    if mapping is None:
        mapping = ProductMapping(
            supplier_product_id=payload.supplier_product_id,
            marketplace_offer_id=offer.id,
            enabled=payload.mapping_enabled,
            marketplace_variant_id=payload.marketplace_variant_id,
            marketplace_variant_name=payload.marketplace_variant_name,
            marketplace_commission_percent=payload.marketplace_commission_percent,
            payment_system_commission_percent=payload.payment_system_commission_percent,
            min_margin_percent=payload.min_margin_percent,
            min_profit_usd=payload.min_profit_usd,
            max_price=payload.max_price,
            manage_stock_quantity=payload.manage_stock_quantity,
            min_purchase_quantity=payload.min_purchase_quantity,
        )
        db.add(mapping)
        mapping_created = True
    else:
        mapping.enabled = payload.mapping_enabled
        mapping.marketplace_variant_id = payload.marketplace_variant_id
        mapping.marketplace_variant_name = payload.marketplace_variant_name
        mapping.marketplace_commission_percent = payload.marketplace_commission_percent
        mapping.payment_system_commission_percent = payload.payment_system_commission_percent
        mapping.min_margin_percent = payload.min_margin_percent
        mapping.min_profit_usd = payload.min_profit_usd
        mapping.max_price = payload.max_price
        mapping.manage_stock_quantity = payload.manage_stock_quantity
        mapping.min_purchase_quantity = payload.min_purchase_quantity
        db.add(mapping)

    db.commit()
    db.refresh(offer)
    db.refresh(mapping)

    return {
        "status": "ok",
        "offer_created": offer_created,
        "mapping_created": mapping_created,
        "offer": {
            "id": offer.id,
            "marketplace": offer.marketplace,
            "marketplace_offer_id": offer.marketplace_offer_id,
            "supplier_product_id": offer.supplier_product_id,
            "title": offer.title,
            "price": _serialize_decimal(offer.price),
            "currency": offer.currency,
            "is_enabled": offer.is_enabled,
            "manually_disabled": offer.manually_disabled,
            "requires_buyer_input": offer.requires_buyer_input,
            "buyer_input_label": offer.buyer_input_label,
            "buyer_input_help": offer.buyer_input_help,
        },
        "mapping": {
            "id": mapping.id,
            "enabled": mapping.enabled,
            "supplier_product_id": mapping.supplier_product_id,
            "marketplace_offer_id": mapping.marketplace_offer_id,
            "marketplace_variant_id": mapping.marketplace_variant_id,
            "marketplace_variant_name": mapping.marketplace_variant_name,
            "marketplace_commission_percent": _serialize_decimal(mapping.marketplace_commission_percent),
            "payment_system_commission_percent": _serialize_decimal(
                mapping.payment_system_commission_percent
            ),
            "manage_stock_quantity": mapping.manage_stock_quantity,
            "min_purchase_quantity": mapping.min_purchase_quantity,
        },
    }


@app.post("/offers/auto-link-recharge-denominations")
def auto_link_recharge_denominations(
    payload: AutoLinkRechargeDenominationsRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Auto-match GGSEL/Digiseller variants to G-Engine denominations by numeric quantity.

    Handles additive notation: GGSEL variant '35 Diamonds' matches G-Engine denomination
    '32 + 3 Diamonds' because 32+3=35.
    """
    service_prefix = f"recharge:{payload.supplier_service_id}:"
    denom_stmt = select(SupplierProduct).where(
        SupplierProduct.supplier == "gengine",
        SupplierProduct.supplier_product_id.like(service_prefix + "%"),
    )
    denominations = db.execute(denom_stmt).scalars().all()
    if not denominations:
        raise HTTPException(
            status_code=404,
            detail=f"No G-Engine denominations found for service '{payload.supplier_service_id}'.",
        )

    denom_by_qty: dict[Decimal, SupplierProduct] = {}
    for denom in denominations:
        qty = _parse_additive_quantity(denom.denomination)
        if qty is not None:
            denom_by_qty[qty] = denom

    offer_stmt = select(MarketplaceOffer).where(
        MarketplaceOffer.marketplace == payload.marketplace,
        MarketplaceOffer.marketplace_offer_id == payload.marketplace_offer_id,
    )
    offer = db.execute(offer_stmt).scalars().first()
    if offer is None:
        raise HTTPException(
            status_code=404,
            detail="Marketplace offer not found. Create it first via /offers/manual-link.",
        )

    results: list[dict[str, Any]] = []
    matched = 0
    unmatched = 0

    for variant in payload.variants:
        variant_qty = _parse_additive_quantity(variant.variant_name)
        matched_denom = denom_by_qty.get(variant_qty) if variant_qty is not None else None

        if matched_denom is None:
            results.append({
                "variant_id": variant.variant_id,
                "variant_name": variant.variant_name,
                "status": "unmatched",
                "reason": "no_denomination_with_matching_quantity",
            })
            unmatched += 1
            continue

        if payload.dry_run:
            results.append({
                "variant_id": variant.variant_id,
                "variant_name": variant.variant_name,
                "status": "would_link",
                "supplier_product_id": matched_denom.id,
                "denomination": matched_denom.denomination,
                "cost_usd": _serialize_decimal(matched_denom.cost_usd),
            })
            matched += 1
            continue

        mapping_stmt = select(ProductMapping).where(
            ProductMapping.supplier_product_id == matched_denom.id,
            ProductMapping.marketplace_offer_id == offer.id,
        )
        mapping = db.execute(mapping_stmt).scalars().first()
        created = False
        if mapping is None:
            mapping = ProductMapping(
                supplier_product_id=matched_denom.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=variant.variant_id,
                marketplace_variant_name=variant.variant_name,
                marketplace_commission_percent=payload.marketplace_commission_percent,
                payment_system_commission_percent=payload.payment_system_commission_percent,
                manage_stock_quantity=payload.manage_stock_quantity,
                min_purchase_quantity=payload.min_purchase_quantity,
            )
            db.add(mapping)
            created = True
        else:
            mapping.marketplace_variant_id = variant.variant_id
            mapping.marketplace_variant_name = variant.variant_name
            if payload.marketplace_commission_percent is not None:
                mapping.marketplace_commission_percent = payload.marketplace_commission_percent
            if payload.payment_system_commission_percent is not None:
                mapping.payment_system_commission_percent = payload.payment_system_commission_percent
            mapping.manage_stock_quantity = payload.manage_stock_quantity
            if payload.min_purchase_quantity is not None:
                mapping.min_purchase_quantity = payload.min_purchase_quantity
            db.add(mapping)

        results.append({
            "variant_id": variant.variant_id,
            "variant_name": variant.variant_name,
            "status": "created" if created else "updated",
            "supplier_product_id": matched_denom.id,
            "denomination": matched_denom.denomination,
            "cost_usd": _serialize_decimal(matched_denom.cost_usd),
        })
        matched += 1

    if not payload.dry_run:
        db.commit()

    return {
        "status": "ok",
        "dry_run": payload.dry_run,
        "matched": matched,
        "unmatched": unmatched,
        "results": results,
    }
