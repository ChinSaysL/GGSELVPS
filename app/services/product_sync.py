from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.clients import DigisellerClient, GEngineClient, GGSELClient
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct, SyncRun
from app.pricing import PricingConfigError, calculate_final_price_usd, convert_usd_to_currency
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class ListingTemplateError(ValueError):
    pass


class SupplierClientProtocol(Protocol):
    def list_products(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class MarketplaceClientProtocol(Protocol):
    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class NormalizedSupplierProduct:
    supplier_product_id: str
    title: str
    category: str | None
    denomination: str | None
    region: str | None
    description: str | None
    cost_usd: Decimal
    supplier_stock: str | None
    is_available: bool
    thumbnail_url: str | None
    raw_payload_json: str


class _SafeTemplateValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        _ = key
        return ""


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


def _extract_currency_code(*values: str | None) -> str | None:
    pattern = re.compile(r"\b(USD|EUR|RUB|UAH|TRY|GBP|KZT)\b", flags=re.IGNORECASE)
    for value in values:
        if not value:
            continue
        match = pattern.search(value)
        if match:
            return match.group(1).upper()
    return None


def _extract_denomination_value(*values: str | None) -> str | None:
    pattern = re.compile(r"\b(\d+(?:[.,]\d+)?)\b")
    for value in values:
        if not value:
            continue
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def _render_template(template: str, values: dict[str, str], *, multiline: bool) -> str:
    rendered = template.format_map(_SafeTemplateValues(values)).strip()
    if not rendered:
        raise ListingTemplateError("Listing template rendered to an empty string.")
    if multiline:
        return rendered
    return " ".join(rendered.split())


def _build_listing_values(product: NormalizedSupplierProduct, settings: Settings) -> dict[str, str]:
    denomination = (
        _extract_denomination_value(product.denomination)
        or _extract_denomination_value(product.title)
        or "N/A"
    )
    currency = (
        _extract_currency_code(product.denomination, product.title)
        or settings.default_currency.upper()
    )
    region = (product.region or "Global").strip() or "Global"

    return {
        "supplier_title": product.title,
        "denomination": denomination,
        "currency": currency,
        "region": region,
        "category": product.category or "Gift Cards",
        "delivery_type": "Auto code delivery (24/7)",
        "delivery_type_ru": "Автовыдача кода (24/7)",
    }


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
    description = _first_non_empty_str(raw_product, ("description", "details", "note"))
    supplier_stock = _first_non_empty_str(raw_product, ("stock", "supplier_stock", "quantity", "available"))
    is_available = _parse_bool(raw_product.get("is_available"), default=True)
    thumbnail_url = _first_non_empty_str(
        raw_product,
        (
            "thumbnail_url",
            "thumbnail",
            "image_url",
            "image",
            "img",
            "icon",
            "picture",
            "photo",
            "logo",
            "cover",
        ),
    )

    return NormalizedSupplierProduct(
        supplier_product_id=supplier_product_id,
        title=title,
        category=category,
        denomination=denomination,
        region=region,
        description=description,
        cost_usd=cost_value,
        supplier_stock=supplier_stock,
        is_available=is_available,
        thumbnail_url=thumbnail_url,
        raw_payload_json=json.dumps(raw_product, ensure_ascii=True, separators=(",", ":")),
    )


def _validate_listing_requirements(product: NormalizedSupplierProduct) -> list[str]:
    errors: list[str] = []
    if not product.title:
        errors.append("missing_title")
    if product.cost_usd <= Decimal("0"):
        errors.append("invalid_cost")
    return errors


def _resolve_marketplace_offer_id(payload: dict[str, Any]) -> str | None:
    candidate_keys = (
        "marketplace_offer_id",
        "offer_id",
        "id",
        "product_id",
        "item_id",
    )
    for key in candidate_keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    data = payload.get("data")
    if isinstance(data, dict):
        for key in candidate_keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

    content = payload.get("content")
    if isinstance(content, dict):
        for key in candidate_keys:
            value = content.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _build_offer_payload(
    product: NormalizedSupplierProduct,
    settings: Settings,
) -> dict[str, Any]:
    calculation = calculate_final_price_usd(
        product.cost_usd,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
    )

    currency = settings.default_currency.upper()
    marketplace_price = calculation.final_price_usd
    if currency == "RUB":
        marketplace_price = convert_usd_to_currency(
            amount_usd=calculation.final_price_usd,
            target_currency="RUB",
            manual_usd_rub_rate=(
                Decimal(str(settings.manual_usd_rub_rate))
                if settings.manual_usd_rub_rate is not None
                else None
            ),
        )

    values = _build_listing_values(product, settings)

    title_en = _render_template(settings.listing_title_template_en, values, multiline=False)
    title_ru = _render_template(settings.listing_title_template_ru, values, multiline=False)

    description_en = _render_template(
        settings.listing_description_template_en,
        values,
        multiline=True,
    )
    description_ru = _render_template(
        settings.listing_description_template_ru,
        values,
        multiline=True,
    )

    title_localized = [
        {"locale": "en-US", "value": title_en},
        {"locale": "ru-RU", "value": title_ru},
    ]
    description_localized = [
        {"locale": "en-US", "value": description_en},
        {"locale": "ru-RU", "value": description_ru},
    ]

    return {
        "title": title_en,
        "title_ru": title_ru,
        "description": description_en,
        "description_ru": description_ru,
        "title_localized": title_localized,
        "description_localized": description_localized,
        "category": product.category,
        "price": float(marketplace_price),
        "currency": currency,
        "enabled": False,
        "stock": 0,
        "content_type": settings.digiseller_content_type,
        "category_id": settings.digiseller_category_id,
        "category_owner": settings.digiseller_category_owner,
        "thumbnail_url": product.thumbnail_url,
    }


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


def _find_existing_offer(
    db: Session,
    *,
    supplier_product_id: int,
    marketplace_name: str,
) -> MarketplaceOffer | None:
    mapping_stmt = (
        select(MarketplaceOffer)
        .join(ProductMapping, ProductMapping.marketplace_offer_id == MarketplaceOffer.id)
        .where(
            and_(
                ProductMapping.supplier_product_id == supplier_product_id,
                MarketplaceOffer.marketplace == marketplace_name,
            )
        )
    )
    offer = db.execute(mapping_stmt).scalars().first()
    if offer is not None:
        return offer

    direct_stmt = select(MarketplaceOffer).where(
        and_(
            MarketplaceOffer.marketplace == marketplace_name,
            MarketplaceOffer.supplier_product_id == supplier_product_id,
        )
    )
    return db.execute(direct_stmt).scalars().first()


def _ensure_mapping(
    db: Session,
    *,
    supplier_product_id: int,
    marketplace_offer_id: int,
) -> bool:
    stmt = select(ProductMapping).where(
        and_(
            ProductMapping.supplier_product_id == supplier_product_id,
            ProductMapping.marketplace_offer_id == marketplace_offer_id,
        )
    )
    mapping = db.execute(stmt).scalars().first()
    if mapping is not None:
        return False

    mapping = ProductMapping(
        supplier_product_id=supplier_product_id,
        marketplace_offer_id=marketplace_offer_id,
        enabled=True,
    )
    db.add(mapping)
    db.flush()
    return True


def sync_products(
    *,
    db: Session,
    settings: Settings,
    supplier_client: SupplierClientProtocol,
    marketplace_client: MarketplaceClientProtocol | None,
    marketplace_name: str | None,
) -> dict[str, Any]:
    raw_products = supplier_client.list_products()
    now = datetime.now(timezone.utc)

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
        "mappings_created": 0,
        "skipped": 0,
    }

    for raw in raw_products:
        normalized = _normalize_supplier_product(raw)
        if normalized is None:
            stats["skipped"] += 1
            log_event(logger, "product_sync.product_skipped", reason="missing_core_fields")
            continue

        supplier_product, change_type = _upsert_supplier_product(db, normalized)
        stats["products_imported"] += 1
        stats[f"supplier_products_{change_type}"] += 1
        log_event(
            logger,
            "product_sync.product_upserted",
            supplier_product_id=normalized.supplier_product_id,
            action=change_type,
        )

        if marketplace_client is None or marketplace_name is None:
            continue

        validation_errors = _validate_listing_requirements(normalized)
        if validation_errors:
            stats["skipped"] += 1
            log_event(
                logger,
                "product_sync.offer_skipped",
                supplier_product_id=normalized.supplier_product_id,
                reason="missing_required_marketplace_fields",
                errors=validation_errors,
            )
            continue

        try:
            payload = _build_offer_payload(normalized, settings)
        except (PricingConfigError, ListingTemplateError) as exc:
            stats["skipped"] += 1
            log_event(
                logger,
                "product_sync.offer_skipped",
                supplier_product_id=normalized.supplier_product_id,
                reason="payload_build_error",
                error=str(exc),
            )
            continue

        existing_offer = _find_existing_offer(
            db,
            supplier_product_id=supplier_product.id,
            marketplace_name=marketplace_name,
        )

        if existing_offer is not None:
            try:
                marketplace_client.update_offer(existing_offer.marketplace_offer_id, payload)
            except NotImplementedError as exc:
                stats["skipped"] += 1
                log_event(
                    logger,
                    "product_sync.offer_skipped",
                    supplier_product_id=normalized.supplier_product_id,
                    reason="marketplace_update_not_implemented",
                    error=str(exc),
                )
                continue

            existing_offer.supplier_product_id = supplier_product.id
            existing_offer.title = payload["title"]
            existing_offer.price = Decimal(str(payload["price"]))
            existing_offer.currency = str(payload["currency"])
            existing_offer.last_synced_at = now
            db.add(existing_offer)

            stats["offers_updated"] += 1
            if _ensure_mapping(
                db,
                supplier_product_id=supplier_product.id,
                marketplace_offer_id=existing_offer.id,
            ):
                stats["mappings_created"] += 1
            log_event(
                logger,
                "product_sync.offer_updated",
                supplier_product_id=normalized.supplier_product_id,
                marketplace_offer_id=existing_offer.marketplace_offer_id,
            )
            continue

        try:
            create_response = marketplace_client.create_offer(payload)
        except NotImplementedError as exc:
            stats["skipped"] += 1
            log_event(
                logger,
                "product_sync.offer_skipped",
                supplier_product_id=normalized.supplier_product_id,
                reason="marketplace_create_not_implemented",
                error=str(exc),
            )
            continue

        marketplace_offer_id = _resolve_marketplace_offer_id(create_response)
        if marketplace_offer_id is None:
            stats["skipped"] += 1
            log_event(
                logger,
                "product_sync.offer_skipped",
                supplier_product_id=normalized.supplier_product_id,
                reason="missing_marketplace_offer_id_in_response",
            )
            continue

        existing_by_remote_id_stmt = select(MarketplaceOffer).where(
            and_(
                MarketplaceOffer.marketplace == marketplace_name,
                MarketplaceOffer.marketplace_offer_id == marketplace_offer_id,
            )
        )
        existing_by_remote_id = db.execute(existing_by_remote_id_stmt).scalars().first()
        if existing_by_remote_id is not None:
            existing_by_remote_id.supplier_product_id = supplier_product.id
            existing_by_remote_id.title = payload["title"]
            existing_by_remote_id.price = Decimal(str(payload["price"]))
            existing_by_remote_id.currency = str(payload["currency"])
            existing_by_remote_id.last_synced_at = now
            db.add(existing_by_remote_id)
            offer_row = existing_by_remote_id
            stats["offers_updated"] += 1
        else:
            offer_row = MarketplaceOffer(
                marketplace=marketplace_name,
                marketplace_offer_id=marketplace_offer_id,
                supplier_product_id=supplier_product.id,
                title=payload["title"],
                price=Decimal(str(payload["price"])),
                currency=str(payload["currency"]),
                is_enabled=False,
                last_synced_at=now,
            )
            db.add(offer_row)
            db.flush()
            stats["offers_created"] += 1

        if _ensure_mapping(
            db,
            supplier_product_id=supplier_product.id,
            marketplace_offer_id=offer_row.id,
        ):
            stats["mappings_created"] += 1

        log_event(
            logger,
            "product_sync.offer_created_or_linked",
            supplier_product_id=normalized.supplier_product_id,
            marketplace_offer_id=marketplace_offer_id,
        )

    return stats


def _resolve_marketplace_runtime(
    settings: Settings,
) -> tuple[str | None, MarketplaceClientProtocol | None, str | None]:
    # Stage 5 assumption:
    # prefer Digiseller when both tokens are present because more offer-management
    # paths are currently confirmed in Swagger.
    if settings.digiseller_api_key:
        return "digiseller", DigisellerClient(settings.digiseller_base_url, settings.digiseller_api_key), None
    if settings.ggsel_api_key:
        return "ggsel", GGSELClient(settings.ggsel_base_url, settings.ggsel_api_key), None
    return None, None, "No marketplace API key configured; products imported without offer sync."


def run_product_sync() -> dict[str, Any]:
    settings = get_settings()
    sync_run: SyncRun | None = None

    gengine_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
    marketplace_name, marketplace_client, marketplace_warning = _resolve_marketplace_runtime(settings)

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
                marketplace_client=marketplace_client,
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
        if marketplace_client is not None:
            marketplace_client.close()
