from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.clients import GEngineClient
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import SupplierProduct, SyncRun
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class SupplierClientProtocol(Protocol):
    def list_products(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class NormalizedSupplierProduct:
    supplier_product_id: str
    title: str
    category: str | None
    denomination: str | None
    region: str | None
    cost_usd: Decimal
    supplier_stock: str | None
    is_available: bool
    raw_payload_json: str


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first_non_empty_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"0", "false", "no", "off", "unavailable", "disabled"}:
        return False
    if normalized in {"1", "true", "yes", "on", "available", "enabled"}:
        return True
    return default


def _normalize_supplier_product(raw_product: dict[str, Any]) -> NormalizedSupplierProduct | None:
    supplier_product_id = _first_non_empty_str(raw_product, ("id", "product_id", "productId", "sku"))
    title = _first_non_empty_str(raw_product, ("title", "name", "product_name"))
    cost_value = (
        _to_decimal(raw_product.get("cost_usd"))
        or _to_decimal(raw_product.get("cost"))
        or _to_decimal(raw_product.get("price_usd"))
        or _to_decimal(raw_product.get("price"))
        or _to_decimal(raw_product.get("amount"))
    )
    if supplier_product_id is None or title is None or cost_value is None:
        return None

    category = _first_non_empty_str(raw_product, ("category", "category_name", "group"))
    denomination = _first_non_empty_str(
        raw_product,
        ("denomination", "nominal", "value", "face_value"),
    )
    region = _first_non_empty_str(raw_product, ("region", "country", "country_code"))
    supplier_stock = _first_non_empty_str(raw_product, ("stock", "supplier_stock", "quantity", "available"))
    is_available = _parse_bool(raw_product.get("is_available"), default=True)

    return NormalizedSupplierProduct(
        supplier_product_id=supplier_product_id,
        title=title,
        category=category,
        denomination=denomination,
        region=region,
        cost_usd=cost_value,
        supplier_stock=supplier_stock,
        is_available=is_available,
        raw_payload_json=json.dumps(raw_product, ensure_ascii=True, separators=(",", ":")),
    )


def _upsert_supplier_product(
    db: Session,
    product: NormalizedSupplierProduct,
) -> tuple[SupplierProduct, str]:
    stmt = select(SupplierProduct).where(
        and_(
            SupplierProduct.supplier == "gengine",
            SupplierProduct.supplier_product_id == product.supplier_product_id,
        )
    )
    existing = db.execute(stmt).scalars().first()
    if existing is None:
        created = SupplierProduct(
            supplier="gengine",
            supplier_product_id=product.supplier_product_id,
            title=product.title,
            category=product.category,
            denomination=product.denomination,
            region=product.region,
            cost_usd=product.cost_usd,
            supplier_stock=product.supplier_stock,
            is_available=product.is_available,
            raw_payload_json=product.raw_payload_json,
        )
        db.add(created)
        db.flush()
        return created, "created"

    existing.title = product.title
    existing.category = product.category
    existing.denomination = product.denomination
    existing.region = product.region
    existing.cost_usd = product.cost_usd
    existing.supplier_stock = product.supplier_stock
    existing.is_available = product.is_available
    existing.raw_payload_json = product.raw_payload_json
    db.add(existing)
    db.flush()
    return existing, "updated"


def sync_products(
    *,
    db: Session,
    settings: Settings,
    supplier_client: SupplierClientProtocol,
    marketplace_name: str | None,
) -> dict[str, Any]:
    # Pricing/listing settings are intentionally ignored here. Product sync never creates
    # or updates marketplace listings; offers must be linked explicitly by an operator.
    _ = settings
    raw_products = supplier_client.list_products()

    stats: dict[str, Any] = {
        "status": "ok",
        "supplier": "gengine",
        "marketplace": marketplace_name or "none",
        "products_received": len(raw_products),
        "products_imported": 0,
        "supplier_products_created": 0,
        "supplier_products_updated": 0,
        "offers_created": 0,
        "offers_updated": 0,
        "offers_skipped": 0,
        "mappings_created": 0,
        "manual_link_required": 0,
        "skipped": 0,
    }

    for raw in raw_products:
        normalized = _normalize_supplier_product(raw)
        if normalized is None:
            stats["skipped"] += 1
            log_event(logger, "product_sync.product_skipped", reason="missing_core_fields")
            continue

        _supplier_product, change_type = _upsert_supplier_product(db, normalized)
        stats["products_imported"] += 1
        stats[f"supplier_products_{change_type}"] += 1
        log_event(
            logger,
            "product_sync.product_upserted",
            supplier_product_id=normalized.supplier_product_id,
            action=change_type,
        )

        if marketplace_name is None:
            continue

        stats["manual_link_required"] += 1
        stats["offers_skipped"] += 1
        log_event(
            logger,
            "product_sync.offer_skipped",
            supplier_product_id=normalized.supplier_product_id,
            marketplace=marketplace_name,
            reason="manual_link_required_auto_listing_disabled",
        )

    return stats


def _resolve_marketplace_runtime(settings: Settings) -> tuple[str | None, str | None]:
    if settings.digiseller_api_key:
        return (
            "digiseller",
            "Automatic listing is disabled. Create marketplace offers manually and link them using /offers/manual-link.",
        )
    if settings.ggsel_api_key:
        return (
            "ggsel",
            "Automatic listing is disabled. Create marketplace offers manually and link them using /offers/manual-link.",
        )
    return None, "No marketplace API key configured; products imported without offer sync."


def run_product_sync() -> dict[str, Any]:
    settings = get_settings()
    sync_run: SyncRun | None = None

    gengine_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
    marketplace_name, marketplace_warning = _resolve_marketplace_runtime(settings)

    try:
        with SessionLocal() as db:
            sync_run = SyncRun(job_name="product_sync", status="running")
            db.add(sync_run)
            db.flush()

            log_event(
                logger,
                "product_sync.started",
                marketplace=marketplace_name or "none",
            )
            stats = sync_products(
                db=db,
                settings=settings,
                supplier_client=gengine_client,
                marketplace_name=marketplace_name,
            )

            if marketplace_warning:
                stats["warning"] = marketplace_warning

            sync_run.status = "success"
            sync_run.finished_at = datetime.now(timezone.utc)
            sync_run.details_json = json.dumps(stats, ensure_ascii=True, separators=(",", ":"))
            db.add(sync_run)
            db.commit()

            log_event(
                logger,
                "product_sync.finished",
                marketplace=stats["marketplace"],
                products_imported=stats["products_imported"],
                offers_created=stats["offers_created"],
                offers_updated=stats["offers_updated"],
                skipped=stats["skipped"],
            )
            return stats
    except Exception as exc:
        with SessionLocal() as db:
            if sync_run is not None and sync_run.id is not None:
                run_row = db.get(SyncRun, sync_run.id)
                if run_row is not None:
                    run_row.status = "failed"
                    run_row.finished_at = datetime.now(timezone.utc)
                    run_row.error_message = str(exc)
                    db.add(run_row)
                    db.commit()
        log_event(logger, "product_sync.failed", error=str(exc))
        raise
    finally:
        gengine_client.close()
