from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients import DigisellerClient, GEngineClient, GGSELClient
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct, SyncRun
from app.pricing import PricingConfigError, calculate_final_price_usd, convert_usd_to_currency
from app.services.fx import resolve_usd_rub_rate
from app.services.product_sync import sync_products
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class MarketplacePriceClientProtocol(Protocol):
    def set_offer_price(self, offer_id: str, price: float, currency: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SupplierRefreshClientProtocol(Protocol):
    def list_product_denominations(
        self,
        product_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _ListSupplierClient:
    products: list[dict[str, Any]]

    def list_products(self) -> list[dict[str, Any]]:
        return self.products

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class _SupplierRefreshReference:
    product_id: str
    fallback_title: str | None
    source_slug: str | None


@dataclass(frozen=True)
class _PricePlan:
    currency: str
    recommended_price: Decimal
    final_price_usd: Decimal
    profit_usd: Decimal
    markup_percent: Decimal
    min_profit_usd: Decimal


class _PriceSyncSkip(RuntimeError):
    pass


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _load_json_object(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        loaded = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _supplier_refresh_reference(
    supplier_product: SupplierProduct,
) -> _SupplierRefreshReference | None:
    if supplier_product.supplier != "gengine":
        return None

    payload = _load_json_object(supplier_product.raw_payload_json)
    product_id = _first_payload_value(
        payload,
        (
            "base_product_id",
            "product_id",
            "productId",
            "shop_product_id",
            "recharge_product_id",
        ),
    )
    if product_id is None:
        return None

    source_slug = _first_payload_value(payload, ("source_slug", "slug"))
    return _SupplierRefreshReference(
        product_id=product_id,
        fallback_title=supplier_product.title,
        source_slug=source_slug,
    )


def _load_enabled_mappings(db: Session) -> list[ProductMapping]:
    stmt = (
        select(ProductMapping)
        .options(
            selectinload(ProductMapping.supplier_product),
            selectinload(ProductMapping.marketplace_offer).selectinload(
                MarketplaceOffer.supplier_product
            ),
        )
        .where(ProductMapping.enabled.is_(True))
        .order_by(ProductMapping.id)
    )
    return list(db.execute(stmt).scalars().all())


def _refresh_linked_supplier_products(
    *,
    db: Session,
    settings: Settings,
    mappings: list[ProductMapping],
    supplier_client: SupplierRefreshClientProtocol | None,
    stats: dict[str, Any],
) -> set[str]:
    references: dict[str, _SupplierRefreshReference] = {}
    for mapping in mappings:
        reference = _supplier_refresh_reference(mapping.supplier_product)
        if reference is not None:
            references.setdefault(reference.product_id, reference)

    if supplier_client is None:
        if references:
            stats["supplier_refresh_skipped"] += len(references)
            stats["details"].append(
                {
                    "action": "supplier_refresh_skipped",
                    "reason": "gengine_price_refresh_client_not_configured",
                    "gengine_product_ids": sorted(references),
                }
            )
        return set(references)

    if not references:
        return set()

    stats["supplier_refresh_products_checked"] = len(references)
    failed_product_ids: set[str] = set()
    for reference in references.values():
        try:
            products = supplier_client.list_product_denominations(
                reference.product_id,
                fallback_title=reference.fallback_title,
                source_slug=reference.source_slug,
            )
        except Exception as exc:  # noqa: BLE001 - keep scheduler resilient per product group.
            stats["supplier_refresh_errors"] += 1
            failed_product_ids.add(reference.product_id)
            stats["details"].append(
                {
                    "action": "supplier_refresh_error",
                    "gengine_product_id": reference.product_id,
                    "reason": str(exc),
                }
            )
            continue

        if not products:
            stats["supplier_refresh_skipped"] += 1
            failed_product_ids.add(reference.product_id)
            stats["details"].append(
                {
                    "action": "supplier_refresh_skipped",
                    "gengine_product_id": reference.product_id,
                    "reason": "empty_response",
                }
            )
            continue

        result = sync_products(
            db=db,
            settings=settings,
            supplier_client=_ListSupplierClient(products),
            marketplace_name=None,
        )
        stats["supplier_products_refreshed"] += int(result["products_imported"])
        stats["supplier_refresh_skipped"] += int(result["skipped"])

    return failed_product_ids


def _calculate_price_plan(
    *,
    settings: Settings,
    mapping: ProductMapping,
    offer: MarketplaceOffer,
    supplier_product: SupplierProduct,
) -> _PricePlan:
    calculation = calculate_final_price_usd(
        supplier_product.cost_usd,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
        markup_percent=mapping.min_margin_percent,
        min_profit_usd=mapping.min_profit_usd,
    )

    currency = (offer.currency or settings.default_currency).upper()
    usd_rub_rate: Decimal | None = None
    if currency == "RUB":
        usd_rub_rate = resolve_usd_rub_rate(settings)

    recommended_price = convert_usd_to_currency(
        calculation.final_price_usd,
        currency,
        manual_usd_rub_rate=usd_rub_rate,
    )

    max_price = _to_decimal(mapping.max_price)
    if max_price is not None and recommended_price > max_price:
        raise _PriceSyncSkip("recommended_price_exceeds_mapping_max_price")

    return _PricePlan(
        currency=currency,
        recommended_price=recommended_price,
        final_price_usd=calculation.final_price_usd,
        profit_usd=calculation.profit_usd,
        markup_percent=calculation.markup_percent,
        min_profit_usd=calculation.min_profit_usd,
    )


def _base_detail(
    *,
    mapping: ProductMapping,
    offer: MarketplaceOffer | None,
    supplier_product: SupplierProduct | None,
) -> dict[str, Any]:
    return {
        "mapping_id": mapping.id,
        "offer_id": offer.id if offer is not None else None,
        "marketplace": offer.marketplace if offer is not None else None,
        "marketplace_offer_id": offer.marketplace_offer_id if offer is not None else None,
        "local_supplier_product_id": (
            supplier_product.id if supplier_product is not None else mapping.supplier_product_id
        ),
        "gengine_supplier_product_id": (
            supplier_product.supplier_product_id if supplier_product is not None else None
        ),
        "title": offer.title if offer is not None else None,
        "denomination": supplier_product.denomination if supplier_product is not None else None,
        "cost_usd": (
            _decimal_to_str(supplier_product.cost_usd) if supplier_product is not None else None
        ),
        "old_price": _decimal_to_str(offer.price) if offer is not None else None,
        "currency": offer.currency if offer is not None else None,
    }


def _skip_detail(
    stats: dict[str, Any],
    detail: dict[str, Any],
    *,
    reason: str,
) -> None:
    stats["skipped"] += 1
    detail["action"] = "skipped"
    detail["reason"] = reason
    stats["details"].append(detail)


def _error_detail(
    stats: dict[str, Any],
    detail: dict[str, Any],
    *,
    reason: str,
) -> None:
    stats["errors"] += 1
    detail["action"] = "error"
    detail["reason"] = reason
    stats["details"].append(detail)


def _group_mappings_by_offer(mappings: list[ProductMapping]) -> dict[int, list[ProductMapping]]:
    grouped: dict[int, list[ProductMapping]] = {}
    for mapping in mappings:
        offer = mapping.marketplace_offer
        if offer is None:
            continue
        grouped.setdefault(int(offer.id), []).append(mapping)
    return grouped


def _digiseller_variant_payload(
    plans: list[tuple[ProductMapping, MarketplaceOffer, _PricePlan]],
) -> tuple[Decimal, list[dict[str, Any]]]:
    base_candidates: list[Decimal] = []
    for mapping, _offer, plan in plans:
        if mapping.marketplace_variant_id is None:
            base_candidates.append(plan.recommended_price)

    if base_candidates:
        base_price = base_candidates[0]
    else:
        base_price = min(plan.recommended_price for _mapping, _offer, plan in plans)

    variants: list[dict[str, Any]] = []
    for mapping, _offer, plan in plans:
        variant_id = mapping.marketplace_variant_id
        if variant_id is None:
            continue
        delta = plan.recommended_price - base_price
        if delta >= Decimal("0"):
            variant_type = 2  # PricePlus
            rate = delta
        else:
            variant_type = 3  # PriceMinus
            rate = -delta
        variants.append(
            {
                "variant_id": int(variant_id),
                "rate": float(rate),
                "type": variant_type,
            }
        )

    return base_price, variants


def sync_offer_prices(
    *,
    db: Session,
    settings: Settings,
    marketplace_clients: dict[str, MarketplacePriceClientProtocol],
    supplier_client: SupplierRefreshClientProtocol | None = None,
    dry_run: bool = False,
    refresh_supplier_prices: bool = True,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "status": "ok",
        "dry_run": dry_run,
        "offers_checked": 0,
        "prices_previewed": 0,
        "prices_updated": 0,
        "skipped": 0,
        "errors": 0,
        "supplier_refresh_products_checked": 0,
        "supplier_products_refreshed": 0,
        "supplier_refresh_skipped": 0,
        "supplier_refresh_errors": 0,
        "details": [],
    }

    mappings = _load_enabled_mappings(db)
    failed_refresh_product_ids: set[str] = set()
    if refresh_supplier_prices:
        failed_refresh_product_ids = _refresh_linked_supplier_products(
            db=db,
            settings=settings,
            mappings=mappings,
            supplier_client=supplier_client,
            stats=stats,
        )
        db.flush()
        db.expire_all()
        mappings = _load_enabled_mappings(db)

    stats["offers_checked"] = len(mappings)
    now = datetime.now(timezone.utc)
    mappings_by_offer = _group_mappings_by_offer(mappings)
    processed_offer_ids: set[int] = set()

    for mapping in mappings:
        offer = mapping.marketplace_offer
        supplier_product = mapping.supplier_product
        detail = _base_detail(
            mapping=mapping,
            offer=offer,
            supplier_product=supplier_product,
        )

        if offer is None or supplier_product is None:
            _skip_detail(stats, detail, reason="mapping_missing_offer_or_supplier")
            continue

        if int(offer.id) in processed_offer_ids:
            continue

        group = mappings_by_offer.get(int(offer.id), [mapping])
        marketplace = offer.marketplace.lower()
        is_digiseller_variant_group = marketplace == "digiseller" and len(group) > 1

        if is_digiseller_variant_group:
            processed_offer_ids.add(int(offer.id))
            if not any(item.marketplace_variant_id is not None for item in group):
                for grouped_mapping in group:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_mapping.marketplace_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    _skip_detail(
                        stats,
                        grouped_detail,
                        reason="multiple_mappings_for_offer_require_variant_ids",
                    )
                continue

            plans: list[tuple[ProductMapping, MarketplaceOffer, _PricePlan]] = []
            group_currencies: set[str] = set()
            for grouped_mapping in group:
                grouped_offer = grouped_mapping.marketplace_offer
                grouped_supplier = grouped_mapping.supplier_product
                grouped_detail = _base_detail(
                    mapping=grouped_mapping,
                    offer=grouped_offer,
                    supplier_product=grouped_supplier,
                )

                if grouped_offer is None or grouped_supplier is None:
                    _skip_detail(stats, grouped_detail, reason="mapping_missing_offer_or_supplier")
                    continue

                refresh_reference = _supplier_refresh_reference(grouped_supplier)
                if (
                    refresh_supplier_prices
                    and refresh_reference is not None
                    and refresh_reference.product_id in failed_refresh_product_ids
                ):
                    _skip_detail(stats, grouped_detail, reason="supplier_price_refresh_failed")
                    continue

                try:
                    grouped_plan = _calculate_price_plan(
                        settings=settings,
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_supplier,
                    )
                except _PriceSyncSkip as exc:
                    _skip_detail(stats, grouped_detail, reason=str(exc))
                    continue
                except PricingConfigError as exc:
                    _skip_detail(stats, grouped_detail, reason=str(exc))
                    continue

                group_currencies.add(grouped_plan.currency)
                plans.append((grouped_mapping, grouped_offer, grouped_plan))

            if not plans:
                continue

            if len(group_currencies) > 1:
                for grouped_mapping, grouped_offer, _grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    _skip_detail(
                        stats,
                        grouped_detail,
                        reason="variant_group_currency_mismatch",
                    )
                continue

            base_price, variant_payload = _digiseller_variant_payload(plans)
            plan_currency = plans[0][2].currency
            client = marketplace_clients.get("digiseller")
            if client is None:
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "currency": grouped_plan.currency,
                        }
                    )
                    _skip_detail(
                        stats,
                        grouped_detail,
                        reason="marketplace_price_client_not_configured",
                    )
                continue
            if dry_run:
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "currency": grouped_plan.currency,
                            "group_base_price": _decimal_to_str(base_price),
                            "variant_id": grouped_mapping.marketplace_variant_id,
                            "variant_name": grouped_mapping.marketplace_variant_name,
                        }
                    )
                    grouped_detail["action"] = "preview"
                    stats["details"].append(grouped_detail)
                    stats["prices_previewed"] += 1
                continue

            try:
                digiseller_client = cast(Any, client)
                digiseller_client.set_offer_price(
                    offer.marketplace_offer_id,
                    float(base_price),
                    plan_currency,
                    variants=variant_payload,
                )
            except NotImplementedError as exc:
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "currency": grouped_plan.currency,
                        }
                    )
                    _skip_detail(stats, grouped_detail, reason=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - keep other offers running.
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "currency": grouped_plan.currency,
                        }
                    )
                    _error_detail(stats, grouped_detail, reason=str(exc))
                continue

            offer.price = base_price
            offer.currency = plan_currency
            offer.last_synced_at = now
            db.add(offer)
            for grouped_mapping, grouped_offer, grouped_plan in plans:
                grouped_detail = _base_detail(
                    mapping=grouped_mapping,
                    offer=grouped_offer,
                    supplier_product=grouped_mapping.supplier_product,
                )
                grouped_detail.update(
                    {
                        "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                        "currency": grouped_plan.currency,
                        "group_base_price": _decimal_to_str(base_price),
                        "variant_id": grouped_mapping.marketplace_variant_id,
                        "variant_name": grouped_mapping.marketplace_variant_name,
                        "variant_mode": True,
                    }
                )
                grouped_detail["action"] = "updated"
                stats["details"].append(grouped_detail)
                stats["prices_updated"] += 1
            continue

        refresh_reference = _supplier_refresh_reference(supplier_product)
        if (
            refresh_supplier_prices
            and refresh_reference is not None
            and refresh_reference.product_id in failed_refresh_product_ids
        ):
            _skip_detail(stats, detail, reason="supplier_price_refresh_failed")
            continue

        try:
            plan = _calculate_price_plan(
                settings=settings,
                mapping=mapping,
                offer=offer,
                supplier_product=supplier_product,
            )
        except _PriceSyncSkip as exc:
            _skip_detail(stats, detail, reason=str(exc))
            continue
        except PricingConfigError as exc:
            _skip_detail(stats, detail, reason=str(exc))
            continue

        detail.update(
            {
                "recommended_price": _decimal_to_str(plan.recommended_price),
                "currency": plan.currency,
                "final_price_usd": _decimal_to_str(plan.final_price_usd),
                "profit_usd": _decimal_to_str(plan.profit_usd),
                "markup_percent": _decimal_to_str(plan.markup_percent),
                "min_profit_usd": _decimal_to_str(plan.min_profit_usd),
            }
        )

        if dry_run:
            stats["prices_previewed"] += 1
            detail["action"] = "preview"
            stats["details"].append(detail)
            continue

        if offer.price == plan.recommended_price and offer.currency.upper() == plan.currency:
            _skip_detail(stats, detail, reason="price_unchanged")
            continue

        client = marketplace_clients.get(marketplace)
        if client is None:
            _skip_detail(stats, detail, reason="marketplace_price_client_not_configured")
            continue

        try:
            client.set_offer_price(
                offer.marketplace_offer_id,
                float(plan.recommended_price),
                plan.currency,
            )
        except NotImplementedError as exc:
            _skip_detail(stats, detail, reason=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one failed offer should not stop the batch.
            _error_detail(stats, detail, reason=str(exc))
            continue

        offer.price = plan.recommended_price
        offer.currency = plan.currency
        offer.last_synced_at = now
        db.add(offer)
        stats["prices_updated"] += 1
        detail["action"] = "updated"
        stats["details"].append(detail)

    if stats["errors"] > 0:
        stats["status"] = "partial"

    return stats


def _build_marketplace_clients(settings: Settings) -> dict[str, MarketplacePriceClientProtocol]:
    clients: dict[str, MarketplacePriceClientProtocol] = {}
    if settings.digiseller_api_key:
        clients["digiseller"] = DigisellerClient(
            settings.digiseller_base_url,
            settings.digiseller_api_key,
        )
    if settings.ggsel_api_key:
        clients["ggsel"] = GGSELClient(settings.ggsel_base_url, settings.ggsel_api_key)
    return clients


def run_price_sync(
    *,
    dry_run: bool = False,
    refresh_supplier_prices: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    sync_run: SyncRun | None = None
    marketplace_clients = _build_marketplace_clients(settings)
    supplier_client: GEngineClient | None = None
    if refresh_supplier_prices and settings.gengine_api_key:
        supplier_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)

    try:
        with SessionLocal() as db:
            sync_run = SyncRun(
                job_name="price_sync_preview" if dry_run else "price_sync",
                status="running",
            )
            db.add(sync_run)
            db.flush()

            log_event(logger, "price_sync.started", dry_run=dry_run)
            stats = sync_offer_prices(
                db=db,
                settings=settings,
                marketplace_clients=marketplace_clients,
                supplier_client=supplier_client,
                dry_run=dry_run,
                refresh_supplier_prices=refresh_supplier_prices,
            )

            sync_run.status = "success" if stats["status"] == "ok" else str(stats["status"])
            sync_run.finished_at = datetime.now(timezone.utc)
            sync_run.details_json = json.dumps(stats, ensure_ascii=True, separators=(",", ":"))
            db.add(sync_run)
            db.commit()

            log_event(
                logger,
                "price_sync.finished",
                dry_run=dry_run,
                offers_checked=stats["offers_checked"],
                prices_previewed=stats["prices_previewed"],
                prices_updated=stats["prices_updated"],
                skipped=stats["skipped"],
                errors=stats["errors"],
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
        log_event(logger, "price_sync.failed", dry_run=dry_run, error=str(exc))
        raise
    finally:
        for client in marketplace_clients.values():
            client.close()
        if supplier_client is not None:
            supplier_client.close()
