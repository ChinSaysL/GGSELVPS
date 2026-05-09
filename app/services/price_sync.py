from __future__ import annotations

import json
import re
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
from app.pricing import (
    PricingConfigError,
    calculate_compounded_price,
    get_markup_percent,
)
from app.services.fx import resolve_usd_rub_rate
from app.services.product_sync import sync_products
from app.services.provider_status import (
    SUPPLIER_PROVIDER_UNAVAILABLE,
    is_temporary_provider_error,
)
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class MarketplacePriceClientProtocol(Protocol):
    def set_offer_price(
        self,
        offer_id: str,
        price: float,
        currency: str,
        *,
        variants: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SupplierRefreshClientProtocol(Protocol):
    def list_product_denominations(
        self,
        product_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_recharge_service_products(
        self,
        service_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
        include_unpriced: bool = False,
    ) -> list[dict[str, Any]]: ...

    def list_gift_app_products(
        self,
        app_id: str,
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
    kind: str
    product_id: str
    fallback_title: str | None
    source_slug: str | None


@dataclass(frozen=True)
class _PricePlan:
    currency: str
    recommended_price: Decimal
    target_net_price: Decimal
    final_price_usd: Decimal
    profit_usd: Decimal
    markup_percent: Decimal
    min_profit_usd: Decimal
    marketplace_commission_percent: Decimal
    payment_system_commission_percent: Decimal


class _PriceSyncSkip(RuntimeError):
    pass


@dataclass(frozen=True)
class _RuntimeQuantityVariant:
    variant_id: int
    quantity: Decimal
    variant_value: str
    option_label: str


def _supplier_refresh_reference_key(reference: _SupplierRefreshReference) -> str:
    return f"{reference.kind}:{reference.product_id}"


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
    source_type = _first_payload_value(payload, ("source_type",))
    supplier_product_id = str(supplier_product.supplier_product_id or "").strip()
    supplier_product_id_prefix = supplier_product_id.split(":", maxsplit=1)[0]
    if (
        source_type in {"gift", "gifts"}
        or supplier_product_id_prefix in {"gift", "gifts"}
        or _first_payload_value(payload, ("gift_app_id", "gift_id")) is not None
    ):
        product_id = _first_payload_value(payload, ("gift_app_id", "gift_id", "base_product_id"))
        if product_id is None and supplier_product_id_prefix in {"gift", "gifts"}:
            parts = supplier_product_id.split(":")
            if len(parts) >= 2 and parts[1]:
                product_id = parts[1]
        if product_id is None:
            return None
        source_slug = _first_payload_value(payload, ("source_slug", "slug"))
        return _SupplierRefreshReference(
            kind="gift",
            product_id=product_id,
            fallback_title=supplier_product.title,
            source_slug=source_slug,
        )

    if source_type == "recharge" or _first_payload_value(payload, ("recharge_product_id",)) is not None:
        product_id = _first_payload_value(payload, ("recharge_product_id", "service_id"))
        if product_id is None:
            return None
        source_slug = _first_payload_value(payload, ("source_slug", "slug"))
        return _SupplierRefreshReference(
            kind="recharge",
            product_id=product_id,
            fallback_title=supplier_product.title,
            source_slug=source_slug,
        )

    product_id = _first_payload_value(
        payload,
        (
            "base_product_id",
            "product_id",
            "productId",
            "shop_product_id",
        ),
    )
    if product_id is None:
        return None

    source_slug = _first_payload_value(payload, ("source_slug", "slug"))
    return _SupplierRefreshReference(
        kind="shop",
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
            references.setdefault(_supplier_refresh_reference_key(reference), reference)

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
            if reference.kind == "recharge":
                products = supplier_client.list_recharge_service_products(
                    reference.product_id,
                    fallback_title=reference.fallback_title,
                    source_slug=reference.source_slug,
                    include_unpriced=False,
                )
            elif reference.kind == "gift":
                products = supplier_client.list_gift_app_products(
                    reference.product_id,
                    fallback_title=reference.fallback_title,
                    source_slug=reference.source_slug,
                )
            else:
                products = supplier_client.list_product_denominations(
                    reference.product_id,
                    fallback_title=reference.fallback_title,
                    source_slug=reference.source_slug,
                )
        except Exception as exc:  # noqa: BLE001 - keep scheduler resilient per product group.
            stats["supplier_refresh_errors"] += 1
            failed_product_ids.add(_supplier_refresh_reference_key(reference))
            reason = SUPPLIER_PROVIDER_UNAVAILABLE if is_temporary_provider_error(exc) else str(exc)
            stats["details"].append(
                {
                    "action": "supplier_refresh_error",
                    "gengine_section": reference.kind,
                    "gengine_product_id": reference.product_id,
                    "reason": reason,
                }
            )
            continue

        if not products:
            stats["supplier_refresh_skipped"] += 1
            if reference.kind != "recharge":
                failed_product_ids.add(_supplier_refresh_reference_key(reference))
            stats["details"].append(
                {
                    "action": "supplier_refresh_skipped",
                    "gengine_section": reference.kind,
                    "gengine_product_id": reference.product_id,
                    "reason": (
                        "recharge_service_has_no_priced_rows_using_existing_cost"
                        if reference.kind == "recharge"
                        else "empty_response"
                    ),
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
    currency = (offer.currency or settings.default_currency).upper()
    usd_rub_rate: Decimal | None = None
    if currency == "RUB":
        usd_rub_rate = resolve_usd_rub_rate(settings)

    marketplace_commission_percent = _to_decimal(mapping.marketplace_commission_percent)
    if marketplace_commission_percent is None:
        marketplace_commission_percent = Decimal("0")
    payment_system_commission_percent = Decimal("0")
    if marketplace_commission_percent < 0:
        raise PricingConfigError("marketplace_commission_percent must be >= 0.")

    if currency == "USD":
        base_amount = Decimal(str(supplier_product.cost_usd))
    elif currency == "RUB":
        if usd_rub_rate is None:
            raise PricingConfigError("USD/RUB rate is required for RUB price calculation.")
        base_amount = Decimal(str(supplier_product.cost_usd)) * usd_rub_rate
    else:
        raise PricingConfigError(f"Unsupported target currency: {currency}")

    effective_markup = (
        mapping.min_margin_percent
        if mapping.min_margin_percent is not None
        else get_markup_percent(supplier_product.cost_usd)
    )

    target_net_price = calculate_compounded_price(
        base_amount,
        markup_percent=effective_markup,
        fee_percent=marketplace_commission_percent,
    )
    recommended_price = target_net_price

    max_price = _to_decimal(mapping.max_price)
    if max_price is not None and recommended_price > max_price:
        raise _PriceSyncSkip("recommended_price_exceeds_mapping_max_price")

    if currency == "USD":
        final_price_usd = recommended_price
    else:
        if usd_rub_rate is None or usd_rub_rate <= 0:
            raise PricingConfigError("USD/RUB rate is required for RUB price calculation.")
        final_price_usd = recommended_price / usd_rub_rate
    profit_usd = final_price_usd - Decimal(str(supplier_product.cost_usd))

    return _PricePlan(
        currency=currency,
        recommended_price=recommended_price,
        target_net_price=target_net_price,
        final_price_usd=final_price_usd,
        profit_usd=profit_usd,
        markup_percent=effective_markup,
        min_profit_usd=Decimal("0"),
        marketplace_commission_percent=marketplace_commission_percent,
        payment_system_commission_percent=payment_system_commission_percent,
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


def _is_digiseller_offer_access_error(reason: str) -> bool:
    text = reason.strip().lower()
    if not text:
        return False
    if "product not found or you do not have edit access" in text:
        return True
    return "product not found" in text and "edit access" in text


def _group_mappings_by_offer(mappings: list[ProductMapping]) -> dict[int, list[ProductMapping]]:
    grouped: dict[int, list[ProductMapping]] = {}
    for mapping in mappings:
        offer = mapping.marketplace_offer
        if offer is None:
            continue
        grouped.setdefault(int(offer.id), []).append(mapping)
    return grouped


def _parse_int_token(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.search(r"\d+", text)
    if match is None:
        return None
    return int(match.group(0))


def _first_non_empty_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_decimal_quantity(value: str | None) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    compact = re.sub(r"[\s\u00A0\u202F]+", "", text)
    match = re.search(r"\d+(?:[.,]\d+)?", compact)
    if match is None:
        return None
    candidate = match.group(0).replace(",", ".")
    try:
        parsed = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _is_quantity_option_label(label: str) -> bool:
    normalized = str(label or "").strip().lower()
    if not normalized:
        return False
    return any(
        token in normalized
        for token in ("quantity", "qty", "count", "amount", "sum", "колич", "сумм")
    )


def _extract_runtime_quantity_variants_from_offer(
    offer_payload: dict[str, Any],
) -> list[_RuntimeQuantityVariant]:
    options = offer_payload.get("options")
    if not isinstance(options, list):
        return []

    quantity_labeled: list[_RuntimeQuantityVariant] = []
    fallback: list[_RuntimeQuantityVariant] = []
    seen_variant_ids: set[int] = set()
    variant_value_keys = ("text", "label", "name", "title", "value")
    variant_id_keys = (
        "variant_id",
        "variantId",
        "id_variant",
        "id_var",
        "var_id",
        "value_id",
        "id_option_value",
        "option_value_id",
        "id",
    )

    for option in options:
        if not isinstance(option, dict):
            continue
        option_label = _first_non_empty_str(option, ("label", "name", "title")) or "Option"
        option_variants = option.get("variants")
        if not isinstance(option_variants, list):
            option_variants = option.get("values")
        if not isinstance(option_variants, list):
            option_variants = option.get("items")
        if not isinstance(option_variants, list):
            option_variants = option.get("options")
        if not isinstance(option_variants, list):
            continue

        for variant in option_variants:
            if not isinstance(variant, dict):
                continue
            variant_id: int | None = None
            for key in variant_id_keys:
                variant_id = _parse_int_token(variant.get(key))
                if variant_id is not None:
                    break
            if variant_id is None or variant_id in seen_variant_ids:
                continue

            variant_value = _first_non_empty_str(variant, variant_value_keys)
            quantity = _parse_decimal_quantity(variant_value)
            if quantity is None:
                continue

            seen_variant_ids.add(variant_id)
            item = _RuntimeQuantityVariant(
                variant_id=variant_id,
                quantity=quantity,
                variant_value=str(variant_value),
                option_label=option_label,
            )
            fallback.append(item)
            if _is_quantity_option_label(option_label):
                quantity_labeled.append(item)

    chosen = quantity_labeled if quantity_labeled else fallback
    return sorted(chosen, key=lambda item: (item.quantity, item.variant_id))


def _resolve_runtime_quantity_variants(
    *,
    client: Any,
    offer_id: str,
    offer_payload: dict[str, Any],
) -> tuple[list[_RuntimeQuantityVariant], str | None]:
    runtime_variants = _extract_runtime_quantity_variants_from_offer(offer_payload)
    if runtime_variants:
        return runtime_variants, None
    if not hasattr(client, "list_offer_options"):
        return [], None
    try:
        options = cast(Any, client).list_offer_options(offer_id)
    except Exception as exc:  # noqa: BLE001
        return [], f"runtime_variant_option_lookup_failed:{exc}"
    if not isinstance(options, list) or not options:
        return [], None
    runtime_variants = _extract_runtime_quantity_variants_from_offer({"options": options})
    if runtime_variants:
        return runtime_variants, None
    return [], None


def _is_marketplace_quantity_recharge_mapping(
    mapping: ProductMapping,
    supplier_product: SupplierProduct,
) -> bool:
    if mapping.marketplace_variant_id is not None:
        return False
    supplier_id = str(supplier_product.supplier_product_id or "").strip()
    parts = supplier_id.split(":")
    if not (len(parts) == 2 and parts[0] == "recharge" and parts[1]):
        return False
    payload = _load_json_object(supplier_product.raw_payload_json)
    if payload.get("source_type") != "recharge" and _first_payload_value(payload, ("recharge_product_id",)) is None:
        return False
    quantity_source = str(payload.get("recharge_quantity_source") or "").strip().lower()
    return quantity_source == "marketplace_quantity"


def _runtime_variant_price_payload(
    prices_by_variant_id: dict[int, Decimal],
) -> tuple[Decimal, list[dict[str, Any]]]:
    base_price = min(prices_by_variant_id.values())
    variants: list[dict[str, Any]] = []
    for variant_id, price in sorted(prices_by_variant_id.items()):
        delta = price - base_price
        if delta < Decimal("0"):
            delta = Decimal("0")
        variants.append(
            {
                "variant_id": int(variant_id),
                "rate": float(delta.quantize(Decimal("0.01"))),
                "type": 2,
            }
        )
    return base_price, variants


def _filter_mappings_by_marketplace_offer_ids(
    mappings: list[ProductMapping],
    offer_ids: set[str],
) -> list[ProductMapping]:
    if not offer_ids:
        return mappings
    filtered: list[ProductMapping] = []
    for mapping in mappings:
        offer = mapping.marketplace_offer
        if offer is None:
            continue
        offer_id = str(offer.marketplace_offer_id or "").strip()
        if offer_id in offer_ids:
            filtered.append(mapping)
    return filtered


def _marketplace_variant_payload(
    plans: list[tuple[ProductMapping, MarketplaceOffer, _PricePlan]],
) -> tuple[Decimal, list[dict[str, Any]]]:
    # Digiseller/GGSEL use fixed base product price + per-variant modifiers.
    # Keep base anchored at the minimum recommended absolute price so every
    # modifier can be represented as a non-negative PricePlus value.
    base_price = min(plan.recommended_price for _mapping, _offer, plan in plans)

    # Check whether any mapping carries an edition_variant_id (2D listing).
    has_edition_variants = any(
        getattr(mapping, "marketplace_edition_variant_id", None) is not None
        for mapping, _offer, _plan in plans
    )

    if has_edition_variants:
        # 2D (Edition × Region) pricing.
        # Digiseller's additive model: displayed = base + edition_rate + region_rate.
        # Strategy:
        #   region_rate[r] = min_e(price(e, r)) - base_price  (cheapest edition per region)
        #   edition_rate[e] = avg_r(price(e, r) - min_e'(price(e', r)))  (average markup above cheapest)
        # This makes the cheapest edition per region always exact, with other editions approximate.

        # Collect per-region minimum prices
        min_price_by_region: dict[int, Decimal] = {}
        for mapping, _offer, plan in plans:
            region_vid = mapping.marketplace_variant_id
            if region_vid is None:
                continue
            rvid = int(region_vid)
            price = plan.recommended_price
            if rvid not in min_price_by_region or price < min_price_by_region[rvid]:
                min_price_by_region[rvid] = price

        # Region rates: cheapest edition price for that region minus global base
        region_rates: dict[int, Decimal] = {
            rvid: max(Decimal("0"), (p - base_price).quantize(Decimal("0.01")))
            for rvid, p in min_price_by_region.items()
        }

        # Edition rates: average markup above cheapest-edition price per region
        edition_markup_sum: dict[int, Decimal] = {}
        edition_markup_count: dict[int, int] = {}
        for mapping, _offer, plan in plans:
            region_vid = mapping.marketplace_variant_id
            edition_vid_raw = getattr(mapping, "marketplace_edition_variant_id", None)
            if region_vid is None or edition_vid_raw is None:
                continue
            rvid = int(region_vid)
            evid = int(edition_vid_raw)
            markup = max(Decimal("0"), plan.recommended_price - min_price_by_region[rvid])
            edition_markup_sum[evid] = edition_markup_sum.get(evid, Decimal("0")) + markup
            edition_markup_count[evid] = edition_markup_count.get(evid, 0) + 1

        edition_rates: dict[int, Decimal] = {
            evid: (edition_markup_sum[evid] / edition_markup_count[evid]).quantize(Decimal("0.01"))
            for evid in edition_markup_sum
            if edition_markup_count[evid] > 0
        }

        all_rates: dict[int, Decimal] = {**region_rates, **edition_rates}
        variants: list[dict[str, Any]] = [
            {"variant_id": vid, "rate": float(rate), "type": 2}
            for vid, rate in sorted(all_rates.items())
        ]
        return base_price, variants

    # 1D case: collect minimum delta per variant_id.
    # Multiple mappings can share the same variant_id (e.g. multi-edition products
    # where region variants are reused). Sending duplicates to the API fails; use
    # the minimum delta so the variant price reflects the cheapest edition for that region.
    min_delta_by_variant: dict[int, Decimal] = {}
    for mapping, _offer, plan in plans:
        variant_id = mapping.marketplace_variant_id
        if variant_id is None:
            continue
        delta = plan.recommended_price - base_price
        if delta < Decimal("0"):
            delta = Decimal("0")
        delta = delta.quantize(Decimal("0.01"))
        vid = int(variant_id)
        if vid not in min_delta_by_variant or delta < min_delta_by_variant[vid]:
            min_delta_by_variant[vid] = delta

    variants = [
        {"variant_id": vid, "rate": float(delta), "type": 2}
        for vid, delta in sorted(min_delta_by_variant.items())
    ]

    return base_price, variants


def _variant_rates_by_id(variant_payload: list[dict[str, Any]]) -> dict[int, Decimal]:
    rates: dict[int, Decimal] = {}
    for item in variant_payload:
        variant_id = _parse_int_token(item.get("variant_id"))
        rate = _to_decimal(item.get("rate"))
        if variant_id is None or rate is None:
            continue
        rates[int(variant_id)] = rate
    return rates


def sync_offer_prices(
    *,
    db: Session,
    settings: Settings,
    marketplace_clients: dict[str, MarketplacePriceClientProtocol],
    supplier_client: SupplierRefreshClientProtocol | None = None,
    dry_run: bool = False,
    refresh_supplier_prices: bool = True,
    force_marketplace_offer_ids: set[str] | None = None,
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

    forced_offer_ids = {
        str(offer_id).strip()
        for offer_id in (force_marketplace_offer_ids or set())
        if str(offer_id).strip()
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
        db.commit()
        db.expire_all()
        mappings = _load_enabled_mappings(db)

    mappings = _filter_mappings_by_marketplace_offer_ids(mappings, forced_offer_ids)
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
        supports_variant_price_group = marketplace in {"digiseller", "ggsel"} and len(group) > 1

        runtime_quantity_mode = (
            marketplace == "digiseller"
            and len(group) == 1
            and _is_marketplace_quantity_recharge_mapping(mapping, supplier_product)
        )

        if runtime_quantity_mode:
            client = marketplace_clients.get(marketplace)
            if client is None:
                _skip_detail(stats, detail, reason="marketplace_price_client_not_configured")
                continue
            if not hasattr(client, "get_offer_by_id"):
                _skip_detail(stats, detail, reason="marketplace_offer_lookup_not_supported")
                continue

            try:
                raw_offer = cast(Any, client).get_offer_by_id(offer.marketplace_offer_id)
            except Exception as exc:  # noqa: BLE001
                _error_detail(stats, detail, reason=f"runtime_variant_lookup_failed:{exc}")
                continue
            if not isinstance(raw_offer, dict):
                _skip_detail(stats, detail, reason="runtime_variant_lookup_empty")
                continue

            runtime_variants, option_error = _resolve_runtime_quantity_variants(
                client=client,
                offer_id=offer.marketplace_offer_id,
                offer_payload=raw_offer,
            )
            if option_error:
                _error_detail(stats, detail, reason=option_error)
                continue
            if not runtime_variants:
                _skip_detail(stats, detail, reason="runtime_quantity_variants_not_found")
                continue

            try:
                marketplace_commission_percent = _to_decimal(mapping.marketplace_commission_percent)
                if marketplace_commission_percent is None:
                    marketplace_commission_percent = Decimal("0")
                if marketplace_commission_percent < 0:
                    raise PricingConfigError("marketplace_commission_percent must be >= 0.")
                effective_markup = (
                    mapping.min_margin_percent
                    if mapping.min_margin_percent is not None
                    else get_markup_percent(supplier_product.cost_usd)
                )
                if effective_markup < 0:
                    raise PricingConfigError("markup_percent must be >= 0.")

                currency = (offer.currency or settings.default_currency).upper()
                usd_rub_rate: Decimal | None = None
                if currency == "RUB":
                    usd_rub_rate = resolve_usd_rub_rate(settings)

                prices_by_variant_id: dict[int, Decimal] = {}
                for runtime_variant in runtime_variants:
                    base_amount_usd = Decimal(str(supplier_product.cost_usd)) * runtime_variant.quantity
                    if currency == "USD":
                        base_amount = base_amount_usd
                    elif currency == "RUB":
                        if usd_rub_rate is None:
                            raise PricingConfigError(
                                "USD/RUB rate is required for RUB price calculation."
                            )
                        base_amount = base_amount_usd * usd_rub_rate
                    else:
                        raise PricingConfigError(f"Unsupported target currency: {currency}")

                    prices_by_variant_id[runtime_variant.variant_id] = calculate_compounded_price(
                        base_amount,
                        markup_percent=effective_markup,
                        fee_percent=marketplace_commission_percent,
                    )
            except PricingConfigError as exc:
                _skip_detail(stats, detail, reason=str(exc))
                continue

            base_price, variant_payload = _runtime_variant_price_payload(prices_by_variant_id)
            detail.update(
                {
                    "runtime_variant_mode": True,
                    "runtime_variant_count": len(runtime_variants),
                    "group_base_price": _decimal_to_str(base_price),
                }
            )

            if dry_run:
                detail["action"] = "preview"
                stats["details"].append(detail)
                stats["prices_previewed"] += 1
                continue

            try:
                update_response = client.set_offer_price(
                    offer.marketplace_offer_id,
                    float(base_price),
                    currency,
                    variants=variant_payload,
                )
            except NotImplementedError as exc:
                _skip_detail(stats, detail, reason=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one failed offer should not stop the batch.
                if marketplace == "digiseller" and _is_digiseller_offer_access_error(str(exc)):
                    mapping.enabled = False
                    db.add(mapping)
                    _skip_detail(
                        stats,
                        detail,
                        reason="marketplace_offer_not_accessible_mapping_disabled",
                    )
                    continue
                _error_detail(stats, detail, reason=str(exc))
                continue

            response_status = ""
            task_id: str | None = None
            if isinstance(update_response, dict):
                response_status = str(update_response.get("status") or "").strip().lower()
                task_id_value = update_response.get("task_id")
                if task_id_value is not None:
                    task_text = str(task_id_value).strip()
                    task_id = task_text or None
            if response_status == "pending":
                pending_reason = "marketplace_async_task_pending"
                if task_id:
                    pending_reason = f"{pending_reason}:{task_id}"
                _skip_detail(stats, detail, reason=pending_reason)
                continue

            offer.price = base_price
            offer.currency = currency
            offer.last_synced_at = now
            db.add(offer)
            stats["prices_updated"] += 1
            detail["action"] = "updated"
            stats["details"].append(detail)
            continue

        if supports_variant_price_group:
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
                    and _supplier_refresh_reference_key(refresh_reference)
                    in failed_refresh_product_ids
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

            base_price, variant_payload = _marketplace_variant_payload(plans)
            variant_rates = _variant_rates_by_id(variant_payload)
            plan_currency = plans[0][2].currency
            client = marketplace_clients.get(marketplace)
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
                            "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                            "currency": grouped_plan.currency,
                            "marketplace_commission_percent": _decimal_to_str(
                                grouped_plan.marketplace_commission_percent
                            ),
                            "payment_system_commission_percent": _decimal_to_str(
                                grouped_plan.payment_system_commission_percent
                            ),
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
                            "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                            "currency": grouped_plan.currency,
                            "group_base_price": _decimal_to_str(base_price),
                            "variant_id": grouped_mapping.marketplace_variant_id,
                            "variant_name": grouped_mapping.marketplace_variant_name,
                            "variant_price_modifier": _decimal_to_str(
                                variant_rates.get(int(grouped_mapping.marketplace_variant_id))
                                if grouped_mapping.marketplace_variant_id is not None
                                else None
                            ),
                            "marketplace_commission_percent": _decimal_to_str(
                                grouped_plan.marketplace_commission_percent
                            ),
                            "payment_system_commission_percent": _decimal_to_str(
                                grouped_plan.payment_system_commission_percent
                            ),
                        }
                    )
                    grouped_detail["action"] = "preview"
                    stats["details"].append(grouped_detail)
                    stats["prices_previewed"] += 1
                continue

            try:
                update_response = client.set_offer_price(
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
                            "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                            "currency": grouped_plan.currency,
                            "marketplace_commission_percent": _decimal_to_str(
                                grouped_plan.marketplace_commission_percent
                            ),
                            "payment_system_commission_percent": _decimal_to_str(
                                grouped_plan.payment_system_commission_percent
                            ),
                        }
                    )
                    _skip_detail(stats, grouped_detail, reason=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - keep other offers running.
                is_access_error = marketplace == "digiseller" and _is_digiseller_offer_access_error(
                    str(exc)
                )
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                            "currency": grouped_plan.currency,
                            "marketplace_commission_percent": _decimal_to_str(
                                grouped_plan.marketplace_commission_percent
                            ),
                            "payment_system_commission_percent": _decimal_to_str(
                                grouped_plan.payment_system_commission_percent
                            ),
                        }
                    )
                    if is_access_error:
                        grouped_mapping.enabled = False
                        db.add(grouped_mapping)
                        _skip_detail(
                            stats,
                            grouped_detail,
                            reason="marketplace_offer_not_accessible_mapping_disabled",
                        )
                    else:
                        _error_detail(stats, grouped_detail, reason=str(exc))
                continue

            response_status = ""
            group_task_id: str | None = None
            if isinstance(update_response, dict):
                response_status = str(update_response.get("status") or "").strip().lower()
                task_id_value = update_response.get("task_id")
                if task_id_value is not None:
                    task_text = str(task_id_value).strip()
                    group_task_id = task_text or None
            if response_status == "pending":
                pending_reason = "marketplace_async_task_pending"
                if group_task_id:
                    pending_reason = f"{pending_reason}:{group_task_id}"
                for grouped_mapping, grouped_offer, grouped_plan in plans:
                    grouped_detail = _base_detail(
                        mapping=grouped_mapping,
                        offer=grouped_offer,
                        supplier_product=grouped_mapping.supplier_product,
                    )
                    grouped_detail.update(
                        {
                            "recommended_price": _decimal_to_str(grouped_plan.recommended_price),
                            "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                            "currency": grouped_plan.currency,
                            "group_base_price": _decimal_to_str(base_price),
                            "variant_id": grouped_mapping.marketplace_variant_id,
                            "variant_name": grouped_mapping.marketplace_variant_name,
                            "variant_mode": True,
                            "variant_price_modifier": _decimal_to_str(
                                variant_rates.get(int(grouped_mapping.marketplace_variant_id))
                                if grouped_mapping.marketplace_variant_id is not None
                                else None
                            ),
                            "task_id": group_task_id,
                            "marketplace_commission_percent": _decimal_to_str(
                                grouped_plan.marketplace_commission_percent
                            ),
                            "payment_system_commission_percent": _decimal_to_str(
                                grouped_plan.payment_system_commission_percent
                            ),
                        }
                    )
                    _skip_detail(stats, grouped_detail, reason=pending_reason)
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
                        "target_net_price": _decimal_to_str(grouped_plan.target_net_price),
                        "currency": grouped_plan.currency,
                        "group_base_price": _decimal_to_str(base_price),
                        "variant_id": grouped_mapping.marketplace_variant_id,
                        "variant_name": grouped_mapping.marketplace_variant_name,
                        "variant_mode": True,
                        "variant_price_modifier": _decimal_to_str(
                            variant_rates.get(int(grouped_mapping.marketplace_variant_id))
                            if grouped_mapping.marketplace_variant_id is not None
                            else None
                        ),
                        "marketplace_commission_percent": _decimal_to_str(
                            grouped_plan.marketplace_commission_percent
                        ),
                        "payment_system_commission_percent": _decimal_to_str(
                            grouped_plan.payment_system_commission_percent
                        ),
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
            and _supplier_refresh_reference_key(refresh_reference) in failed_refresh_product_ids
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
                "target_net_price": _decimal_to_str(plan.target_net_price),
                "currency": plan.currency,
                "final_price_usd": _decimal_to_str(plan.final_price_usd),
                "profit_usd": _decimal_to_str(plan.profit_usd),
                "markup_percent": _decimal_to_str(plan.markup_percent),
                "min_profit_usd": _decimal_to_str(plan.min_profit_usd),
                "marketplace_commission_percent": _decimal_to_str(
                    plan.marketplace_commission_percent
                ),
                "payment_system_commission_percent": _decimal_to_str(
                    plan.payment_system_commission_percent
                ),
            }
        )

        if dry_run:
            stats["prices_previewed"] += 1
            detail["action"] = "preview"
            stats["details"].append(detail)
            continue

        force_push = str(offer.marketplace_offer_id).strip() in forced_offer_ids
        if (
            not force_push
            and offer.price == plan.recommended_price
            and offer.currency.upper() == plan.currency
        ):
            _skip_detail(stats, detail, reason="price_unchanged")
            continue

        client = marketplace_clients.get(marketplace)
        if client is None:
            _skip_detail(stats, detail, reason="marketplace_price_client_not_configured")
            continue

        try:
            update_response = client.set_offer_price(
                offer.marketplace_offer_id,
                float(plan.recommended_price),
                plan.currency,
            )
        except NotImplementedError as exc:
            _skip_detail(stats, detail, reason=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one failed offer should not stop the batch.
            if marketplace == "digiseller" and _is_digiseller_offer_access_error(str(exc)):
                mapping.enabled = False
                db.add(mapping)
                _skip_detail(
                    stats,
                    detail,
                    reason="marketplace_offer_not_accessible_mapping_disabled",
                )
                continue
            _error_detail(stats, detail, reason=str(exc))
            continue

        response_status = ""
        item_task_id: str | None = None
        if isinstance(update_response, dict):
            response_status = str(update_response.get("status") or "").strip().lower()
            task_id_value = update_response.get("task_id")
            if task_id_value is not None:
                task_text = str(task_id_value).strip()
                item_task_id = task_text or None
        if response_status == "pending":
            detail["task_id"] = item_task_id
            pending_reason = "marketplace_async_task_pending"
            if item_task_id:
                pending_reason = f"{pending_reason}:{item_task_id}"
            _skip_detail(stats, detail, reason=pending_reason)
            continue

        offer.price = plan.recommended_price
        offer.currency = plan.currency
        offer.last_synced_at = now
        db.add(offer)
        stats["prices_updated"] += 1
        detail["action"] = "updated"
        stats["details"].append(detail)

    if stats["errors"] > 0 or stats["supplier_refresh_errors"] > 0:
        stats["status"] = "partial"

    return stats


def _build_marketplace_clients(settings: Settings) -> dict[str, MarketplacePriceClientProtocol]:
    clients: dict[str, MarketplacePriceClientProtocol] = {}
    if settings.digiseller_api_key:
        clients["digiseller"] = DigisellerClient(
            settings.digiseller_base_url,
            settings.digiseller_api_key,
            seller_id=settings.digiseller_seller_id,
            task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
            task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
            rate_limit_per_minute=settings.digiseller_rate_limit_per_minute,
        )
    if settings.ggsel_api_key:
        clients["ggsel"] = GGSELClient(
            settings.ggsel_base_url,
            settings.ggsel_api_key,
            seller_id=settings.ggsel_seller_id,
        )
    return clients


def run_price_sync(
    *,
    dry_run: bool = False,
    refresh_supplier_prices: bool = True,
    force_marketplace_offer_ids: set[str] | None = None,
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
            db.commit()

            log_event(logger, "price_sync.started", dry_run=dry_run)
            stats = sync_offer_prices(
                db=db,
                settings=settings,
                marketplace_clients=marketplace_clients,
                supplier_client=supplier_client,
                dry_run=dry_run,
                refresh_supplier_prices=refresh_supplier_prices,
                force_marketplace_offer_ids=force_marketplace_offer_ids,
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
