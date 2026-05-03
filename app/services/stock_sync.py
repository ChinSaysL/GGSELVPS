from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients import DigisellerClient, GEngineClient, GGSELClient
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import BalanceSnapshot, MarketplaceOffer, ProductMapping, SupplierProduct, SyncRun
from app.stock_rules import compute_usable_balance, evaluate_stock_status
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class SupplierBalanceClientProtocol(Protocol):
    def get_balance(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class MarketplaceStockClientProtocol(Protocol):
    def set_offer_enabled(self, offer_id: str, enabled: bool) -> dict[str, Any]: ...

    def close(self) -> None: ...


class MarketplaceVariantStockClientProtocol(MarketplaceStockClientProtocol, Protocol):
    def set_offer_variant_visible(
        self,
        offer_id: str,
        variant_id: int,
        visible: bool,
    ) -> dict[str, Any]: ...


class MarketplaceVariantStateClientProtocol(MarketplaceVariantStockClientProtocol, Protocol):
    def set_offer_variant_state(
        self,
        offer_id: str,
        variant_id: int,
        *,
        visible: bool,
        is_default: bool | None = None,
    ) -> dict[str, Any]: ...


class MarketplaceOfferLookupClientProtocol(MarketplaceStockClientProtocol, Protocol):
    def get_offer_by_id(self, offer_id: str) -> dict[str, Any] | None: ...


class MarketplaceRuntimeVariantClientProtocol(
    MarketplaceVariantStateClientProtocol, MarketplaceOfferLookupClientProtocol, Protocol
):
    pass


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _value_by_path(payload: dict[str, Any], path: tuple[str, ...]) -> object | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _extract_balance_usd(balance_payload: dict[str, Any]) -> Decimal:
    candidate_paths: tuple[tuple[str, ...], ...] = (
        ("balance_usd",),
        ("usd_balance",),
        ("balance",),
        ("data", "balance_usd"),
        ("data", "usd_balance"),
        ("data", "balance"),
        ("data", "amount"),
    )
    for path in candidate_paths:
        value = _value_by_path(balance_payload, path)
        amount = _to_decimal(value)
        if amount is not None:
            return amount

    data = balance_payload.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("currency") or "").strip().upper()
            if currency != "USD":
                continue
            amount = _to_decimal(item.get("amount"))
            if amount is not None:
                return amount

    raise ValueError("Could not parse USD balance from G-Engine response.")


def _load_mappings(db: Session) -> list[ProductMapping]:
    stmt = (
        select(ProductMapping)
        .options(
            selectinload(ProductMapping.supplier_product),
            selectinload(ProductMapping.marketplace_offer),
        )
        .order_by(ProductMapping.id)
    )
    return list(db.execute(stmt).scalars().all())


def _group_mappings_by_offer(mappings: list[ProductMapping]) -> dict[int, list[ProductMapping]]:
    grouped: dict[int, list[ProductMapping]] = {}
    for mapping in mappings:
        offer = mapping.marketplace_offer
        if offer is None:
            continue
        grouped.setdefault(int(offer.id), []).append(mapping)
    return grouped


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


def _base_detail(
    *,
    mapping: ProductMapping,
    offer: MarketplaceOffer | None,
    supplier: SupplierProduct | None,
) -> dict[str, Any]:
    return {
        "mapping_id": mapping.id,
        "offer_id": offer.id if offer is not None else None,
        "marketplace": offer.marketplace if offer is not None else None,
        "marketplace_offer_id": offer.marketplace_offer_id if offer is not None else None,
        "local_supplier_product_id": supplier.id if supplier is not None else mapping.supplier_product_id,
        "gengine_supplier_product_id": (
            supplier.supplier_product_id if supplier is not None else None
        ),
        "supplier_title": supplier.title if supplier is not None else None,
        "denomination": supplier.denomination if supplier is not None else None,
        "supplier_stock": supplier.supplier_stock if supplier is not None else None,
        "cost_usd": str(supplier.cost_usd) if supplier is not None else None,
        "variant_id": mapping.marketplace_variant_id,
        "variant_name": mapping.marketplace_variant_name,
        "manually_disabled": bool(getattr(offer, "manually_disabled", False))
        if offer is not None
        else False,
    }


def _skip_detail(stats: dict[str, Any], detail: dict[str, Any], *, reason: str) -> None:
    stats["skipped"] += 1
    detail["action"] = "skipped"
    detail["reason"] = reason
    stats["details"].append(detail)


def _error_detail(stats: dict[str, Any], detail: dict[str, Any], *, reason: str) -> None:
    stats["errors"] += 1
    detail["action"] = "error"
    detail["reason"] = reason
    stats["details"].append(detail)


def _updated_detail(stats: dict[str, Any], detail: dict[str, Any], *, enabled: bool) -> None:
    detail["action"] = "updated"
    detail["enabled"] = enabled
    stats["details"].append(detail)
    if enabled:
        stats["offers_enabled"] += 1
    else:
        stats["offers_disabled"] += 1


def _status_detail(
    stats: dict[str, Any],
    detail: dict[str, Any],
    *,
    action: str,
    enabled: bool | None = None,
    reason: str | None = None,
) -> None:
    detail["action"] = action
    if enabled is not None:
        detail["enabled"] = enabled
    if reason is not None:
        detail["reason"] = reason
    stats["details"].append(detail)


def _is_pending_response(response: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(response, dict):
        return False, None
    status = str(response.get("status") or "").strip().lower()
    if status != "pending":
        return False, None
    task_id_raw = response.get("task_id")
    if task_id_raw is None:
        return True, None
    task_id = str(task_id_raw).strip()
    return True, task_id or None


def _all_decisions_have_reason(
    decisions: list[tuple[ProductMapping, SupplierProduct, bool, str, Decimal]],
    reason: str,
) -> bool:
    return bool(decisions) and all(item[3] == reason for item in decisions)


def _digiseller_disable_is_already_unavailable_error(reason: str) -> bool:
    text = reason.strip().lower()
    if not text:
        return False
    return "current status of product does not allow the specified action" in text


def _marketplace_status_update_error_is_safe_noop(*, marketplace: str, reason: str) -> bool:
    if marketplace != "digiseller":
        return False
    return _digiseller_disable_is_already_unavailable_error(reason)


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


def _is_marketplace_quantity_recharge_mapping(mapping: ProductMapping, supplier: SupplierProduct) -> bool:
    if mapping.marketplace_variant_id is not None:
        return False
    supplier_id = str(supplier.supplier_product_id or "").strip()
    parts = supplier_id.split(":")
    if not (len(parts) == 2 and parts[0] == "recharge" and parts[1]):
        return False
    try:
        payload = json.loads(supplier.raw_payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        return False
    if payload.get("source_type") != "recharge" and payload.get("recharge_product_id") is None:
        return False
    quantity_source = str(payload.get("recharge_quantity_source") or "").strip().lower()
    return quantity_source == "marketplace_quantity"


def _effective_supplier_cost(supplier: SupplierProduct, mapping: ProductMapping) -> Decimal:
    if mapping.marketplace_variant_id is None:
        return supplier.cost_usd
    try:
        payload = json.loads(supplier.raw_payload_json or "{}")
    except json.JSONDecodeError:
        return supplier.cost_usd
    if not isinstance(payload, dict):
        return supplier.cost_usd
    quantity_source = str(payload.get("recharge_quantity_source") or "").strip().lower()
    if quantity_source != "marketplace_quantity":
        return supplier.cost_usd
    quantity = _parse_decimal_quantity(mapping.marketplace_variant_name)
    if quantity is None or quantity <= 0:
        return supplier.cost_usd
    return supplier.cost_usd * quantity


def _compute_managed_stock_quantity(
    *,
    usable_balance_usd: Decimal,
    cost_per_unit_usd: Decimal,
    min_purchase_quantity: int | None,
) -> int:
    if cost_per_unit_usd <= 0:
        return 0
    max_units = int(usable_balance_usd / cost_per_unit_usd)
    if min_purchase_quantity is not None and min_purchase_quantity > 0:
        if max_units < min_purchase_quantity:
            return 0
    return max(0, max_units)


def _extract_runtime_quantity_variants_from_offer(
    offer_payload: dict[str, Any],
) -> list[tuple[int, Decimal, str]]:
    options = offer_payload.get("options")
    if not isinstance(options, list):
        return []
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

    quantity_labeled: list[tuple[int, Decimal, str]] = []
    fallback: list[tuple[int, Decimal, str]] = []
    seen_variant_ids: set[int] = set()
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
            payload = (variant_id, quantity, str(variant_value))
            fallback.append(payload)
            if _is_quantity_option_label(option_label):
                quantity_labeled.append(payload)

    chosen = quantity_labeled if quantity_labeled else fallback
    return sorted(chosen, key=lambda item: (item[1], item[0]))


def _resolve_runtime_quantity_variants(
    *,
    client: Any,
    offer_id: str,
    offer_payload: dict[str, Any],
) -> tuple[list[tuple[int, Decimal, str]], str | None]:
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


def _set_offer_variant_stock_state(
    *,
    client: MarketplaceVariantStockClientProtocol,
    offer_id: str,
    variant_id: int,
    visible: bool,
    is_default: bool | None,
) -> dict[str, Any]:
    if hasattr(client, "set_offer_variant_state"):
        state_client = cast(MarketplaceVariantStateClientProtocol, client)
        return state_client.set_offer_variant_state(
            offer_id,
            variant_id,
            visible=visible,
            is_default=is_default,
        )

    return client.set_offer_variant_visible(
        offer_id,
        variant_id,
        visible,
    )


def sync_offer_stock(
    *,
    db: Session,
    settings: Settings,
    supplier_balance_client: SupplierBalanceClientProtocol,
    marketplace_clients: dict[str, MarketplaceStockClientProtocol],
    force_marketplace_offer_ids: set[str] | None = None,
) -> dict[str, Any]:
    balance_payload = supplier_balance_client.get_balance()
    balance_usd = _extract_balance_usd(balance_payload)
    usable_balance_usd = compute_usable_balance(balance_usd, settings.safety_balance_percent)

    snapshot = BalanceSnapshot(
        supplier="gengine",
        balance_usd=balance_usd,
        usable_balance_usd=usable_balance_usd,
    )
    db.add(snapshot)
    db.commit()

    stats: dict[str, Any] = {
        "status": "ok",
        "balance_usd": str(balance_usd),
        "usable_balance_usd": str(usable_balance_usd),
        "offers_checked": 0,
        "offers_enabled": 0,
        "offers_disabled": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }

    forced_offer_ids = {
        str(offer_id).strip()
        for offer_id in (force_marketplace_offer_ids or set())
        if str(offer_id).strip()
    }
    mappings = _load_mappings(db)
    mappings = _filter_mappings_by_marketplace_offer_ids(mappings, forced_offer_ids)
    stats["offers_checked"] = len(mappings)
    if not mappings:
        return stats

    now = datetime.now(timezone.utc)
    grouped = _group_mappings_by_offer(mappings)

    for group in grouped.values():
        offer = group[0].marketplace_offer
        if offer is None:
            for mapping in group:
                detail = _base_detail(mapping=mapping, offer=None, supplier=mapping.supplier_product)
                _skip_detail(stats, detail, reason="mapping_missing_offer")
            continue

        decisions: list[tuple[ProductMapping, SupplierProduct, bool, str, Decimal]] = []
        for mapping in group:
            supplier = mapping.supplier_product
            detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
            if supplier is None:
                _skip_detail(stats, detail, reason="mapping_missing_supplier")
                continue
            decision = evaluate_stock_status(
                mapping_enabled=mapping.enabled,
                manually_disabled=offer.manually_disabled,
                supplier_is_available=supplier.is_available,
                supplier_stock=supplier.supplier_stock,
                usable_balance_usd=usable_balance_usd,
                supplier_cost_usd=_effective_supplier_cost(supplier, mapping),
                has_positive_profit=True,
            )
            decisions.append(
                (
                    mapping,
                    supplier,
                    decision.in_stock,
                    decision.reason,
                    decision.required_balance_usd,
                )
            )

        if not decisions:
            continue

        marketplace = offer.marketplace.lower()
        if _all_decisions_have_reason(decisions, "mapping_disabled"):
            offer.is_enabled = False
            offer.last_synced_at = now
            db.add(offer)
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                detail["stock_reason"] = reason
                _skip_detail(
                    stats,
                    detail,
                    reason="mapping_disabled_no_marketplace_update",
                )
            continue

        client = marketplace_clients.get(marketplace)
        if client is None:
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                _skip_detail(stats, detail, reason=f"marketplace_client_not_configured:{reason}")
            continue

        runtime_quantity_variant_mode = (
            marketplace == "digiseller"
            and len(decisions) == 1
            and _is_marketplace_quantity_recharge_mapping(decisions[0][0], decisions[0][1])
            and hasattr(client, "set_offer_variant_visible")
            and hasattr(client, "get_offer_by_id")
        )
        if runtime_quantity_variant_mode:
            runtime_mapping, runtime_supplier, _runtime_in_stock, _runtime_reason, _runtime_required = decisions[0]
            runtime_client = cast(MarketplaceRuntimeVariantClientProtocol, client)
            try:
                raw_offer = runtime_client.get_offer_by_id(offer.marketplace_offer_id)
            except Exception as exc:  # noqa: BLE001
                detail = _base_detail(mapping=runtime_mapping, offer=offer, supplier=runtime_supplier)
                _error_detail(stats, detail, reason=f"runtime_variant_lookup_failed:{exc}")
                continue
            if not isinstance(raw_offer, dict):
                detail = _base_detail(mapping=runtime_mapping, offer=offer, supplier=runtime_supplier)
                _skip_detail(stats, detail, reason="runtime_variant_lookup_empty")
                continue

            runtime_variants, option_error = _resolve_runtime_quantity_variants(
                client=runtime_client,
                offer_id=offer.marketplace_offer_id,
                offer_payload=raw_offer,
            )
            if option_error:
                detail = _base_detail(mapping=runtime_mapping, offer=offer, supplier=runtime_supplier)
                _error_detail(stats, detail, reason=option_error)
                continue
            if not runtime_variants:
                detail = _base_detail(mapping=runtime_mapping, offer=offer, supplier=runtime_supplier)
                _skip_detail(stats, detail, reason="runtime_quantity_variants_not_found")
                continue

            variant_decisions: list[tuple[int, Decimal, bool, str, Decimal]] = []
            runtime_default_variant_id: int | None = None
            for variant_id, quantity, variant_value in runtime_variants:
                decision = evaluate_stock_status(
                    mapping_enabled=runtime_mapping.enabled,
                    manually_disabled=offer.manually_disabled,
                    supplier_is_available=runtime_supplier.is_available,
                    supplier_stock=runtime_supplier.supplier_stock,
                    usable_balance_usd=usable_balance_usd,
                    supplier_cost_usd=runtime_supplier.cost_usd * quantity,
                    has_positive_profit=True,
                )
                if decision.in_stock and runtime_default_variant_id is None:
                    runtime_default_variant_id = variant_id
                variant_decisions.append(
                    (
                        variant_id,
                        quantity,
                        decision.in_stock,
                        decision.reason,
                        decision.required_balance_usd,
                    )
                )

            confirmed_visible_variant = False
            variant_control_failed = False
            ordered_variant_decisions = sorted(
                variant_decisions,
                key=lambda item: (
                    0
                    if runtime_default_variant_id is not None
                    and item[0] == runtime_default_variant_id
                    else 1
                ),
            )

            for variant_id, quantity, in_stock, reason, required_balance in ordered_variant_decisions:
                detail = _base_detail(mapping=runtime_mapping, offer=offer, supplier=runtime_supplier)
                detail["variant_id"] = variant_id
                detail["variant_quantity"] = str(quantity)
                detail["required_balance_usd"] = str(required_balance)
                detail["stock_reason"] = reason

                runtime_desired_default = (
                    bool(
                        runtime_default_variant_id is not None
                        and variant_id == runtime_default_variant_id
                    )
                    if runtime_default_variant_id is not None
                    else False
                )
                try:
                    response = _set_offer_variant_stock_state(
                        client=runtime_client,
                        offer_id=offer.marketplace_offer_id,
                        variant_id=variant_id,
                        visible=in_stock,
                        is_default=runtime_desired_default,
                    )
                except NotImplementedError as exc:
                    variant_control_failed = True
                    _skip_detail(stats, detail, reason=str(exc))
                    continue
                except Exception as exc:  # noqa: BLE001
                    variant_control_failed = True
                    _error_detail(stats, detail, reason=str(exc))
                    continue

                is_pending, task_id = _is_pending_response(response if isinstance(response, dict) else None)
                if is_pending:
                    variant_control_failed = True
                    pending_reason = "marketplace_async_task_pending"
                    if task_id:
                        pending_reason = f"{pending_reason}:{task_id}"
                    _skip_detail(stats, detail, reason=pending_reason)
                    continue

                if in_stock:
                    confirmed_visible_variant = True
                _updated_detail(stats, detail, enabled=in_stock)

            parent_enabled = confirmed_visible_variant and not variant_control_failed
            runtime_parent_detail: dict[str, Any] = {
                "offer_id": offer.id,
                "marketplace": offer.marketplace,
                "marketplace_offer_id": offer.marketplace_offer_id,
                "target_enabled": parent_enabled,
                "variant_group": True,
                "runtime_variant_mode": True,
                "variant_control_failed": variant_control_failed,
            }
            try:
                parent_response = runtime_client.set_offer_enabled(
                    offer.marketplace_offer_id,
                    parent_enabled,
                )
            except NotImplementedError as exc:
                _status_detail(
                    stats,
                    runtime_parent_detail,
                    action="skipped",
                    reason=f"parent_offer_status_not_updated:{str(exc)}",
                )
            except Exception as exc:  # noqa: BLE001
                error_reason = str(exc)
                if _marketplace_status_update_error_is_safe_noop(
                    marketplace=marketplace,
                    reason=error_reason,
                ):
                    _status_detail(
                        stats,
                        runtime_parent_detail,
                        action="skipped",
                        reason=f"parent_offer_status_unchanged:{error_reason}",
                    )
                else:
                    _error_detail(
                        stats,
                        runtime_parent_detail,
                        reason=f"parent_offer_status_update_failed:{error_reason}",
                    )
            else:
                parent_pending, parent_task_id = _is_pending_response(
                    parent_response if isinstance(parent_response, dict) else None
                )
                if parent_pending:
                    parent_reason = "parent_offer_status_async_task_pending"
                    if parent_task_id:
                        parent_reason = f"{parent_reason}:{parent_task_id}"
                    _status_detail(
                        stats,
                        runtime_parent_detail,
                        action="skipped",
                        reason=parent_reason,
                    )
                else:
                    _status_detail(
                        stats,
                        runtime_parent_detail,
                        action="parent_status_updated",
                        enabled=parent_enabled,
                    )

            offer.is_enabled = parent_enabled
            offer.last_synced_at = now
            db.add(offer)
            continue

        has_variant_bindings = any(item[0].marketplace_variant_id is not None for item in decisions)
        has_variant_stock_api = hasattr(client, "set_offer_variant_visible")
        if has_variant_bindings and has_variant_stock_api:
            variant_client = cast(MarketplaceVariantStockClientProtocol, client)
            any_in_stock = False
            default_variant_id: int | None = None
            for mapping, _supplier, in_stock, _reason, _required_balance in decisions:
                if in_stock and mapping.marketplace_variant_id is not None and default_variant_id is None:
                    default_variant_id = int(mapping.marketplace_variant_id)
                if in_stock:
                    any_in_stock = True

            if not any_in_stock:
                no_stock_parent_detail: dict[str, Any] = {
                    "offer_id": offer.id,
                    "marketplace": offer.marketplace,
                    "marketplace_offer_id": offer.marketplace_offer_id,
                    "target_enabled": False,
                    "variant_group": True,
                    "stock_reason": "all_variants_unfulfillable",
                }
                try:
                    parent_response = client.set_offer_enabled(offer.marketplace_offer_id, False)
                except NotImplementedError as exc:
                    _skip_detail(
                        stats,
                        no_stock_parent_detail,
                        reason=f"parent_offer_status_not_updated:{str(exc)}",
                    )
                except Exception as exc:  # noqa: BLE001
                    error_reason = str(exc)
                    if _marketplace_status_update_error_is_safe_noop(
                        marketplace=marketplace,
                        reason=error_reason,
                    ):
                        _skip_detail(
                            stats,
                            no_stock_parent_detail,
                            reason=f"parent_offer_already_unavailable:{error_reason}",
                        )
                    else:
                        _error_detail(
                            stats,
                            no_stock_parent_detail,
                            reason=f"parent_offer_status_update_failed:{error_reason}",
                        )
                else:
                    parent_pending, parent_task_id = _is_pending_response(
                        parent_response if isinstance(parent_response, dict) else None
                    )
                    if parent_pending:
                        parent_reason = "parent_offer_status_async_task_pending"
                        if parent_task_id:
                            parent_reason = f"{parent_reason}:{parent_task_id}"
                        _skip_detail(stats, no_stock_parent_detail, reason=parent_reason)
                    else:
                        _updated_detail(stats, no_stock_parent_detail, enabled=False)

                offer.is_enabled = False
                offer.last_synced_at = now
                db.add(offer)

                for mapping, supplier, _in_stock, reason, required_balance in decisions:
                    detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                    detail["required_balance_usd"] = str(required_balance)
                    detail["stock_reason"] = reason
                    _skip_detail(
                        stats,
                        detail,
                        reason="variant_visibility_skipped_all_variants_unfulfillable",
                    )
                continue

            confirmed_visible_variant = False
            variant_control_failed = False
            ordered_decisions = sorted(
                decisions,
                key=lambda item: (
                    0
                    if default_variant_id is not None
                    and item[0].marketplace_variant_id == default_variant_id
                    else 1
                ),
            )
            for mapping, supplier, in_stock, reason, required_balance in ordered_decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                detail["stock_reason"] = reason
                mapped_variant_id = mapping.marketplace_variant_id
                if mapped_variant_id is None:
                    variant_control_failed = True
                    _skip_detail(stats, detail, reason="variant_id_missing_for_variant_offer")
                    continue

                desired_default: bool | None = None
                if default_variant_id is not None:
                    desired_default = int(mapped_variant_id) == default_variant_id

                try:
                    response = _set_offer_variant_stock_state(
                        client=variant_client,
                        offer_id=offer.marketplace_offer_id,
                        variant_id=int(mapped_variant_id),
                        visible=in_stock,
                        is_default=desired_default,
                    )
                except NotImplementedError as exc:
                    variant_control_failed = True
                    _skip_detail(stats, detail, reason=str(exc))
                    continue
                except Exception as exc:  # noqa: BLE001
                    variant_control_failed = True
                    _error_detail(stats, detail, reason=str(exc))
                    continue

                is_pending, task_id = _is_pending_response(response if isinstance(response, dict) else None)
                if is_pending:
                    variant_control_failed = True
                    pending_reason = "marketplace_async_task_pending"
                    if task_id:
                        pending_reason = f"{pending_reason}:{task_id}"
                    _skip_detail(stats, detail, reason=pending_reason)
                    continue

                if in_stock:
                    confirmed_visible_variant = True
                _updated_detail(stats, detail, enabled=in_stock)

            parent_enabled = confirmed_visible_variant and not variant_control_failed
            parent_detail: dict[str, Any] = {
                "offer_id": offer.id,
                "marketplace": offer.marketplace,
                "marketplace_offer_id": offer.marketplace_offer_id,
                "target_enabled": parent_enabled,
                "raw_any_in_stock": any_in_stock,
                "variant_control_failed": variant_control_failed,
                "variant_group": True,
            }
            try:
                parent_response = client.set_offer_enabled(offer.marketplace_offer_id, parent_enabled)
            except NotImplementedError as exc:
                _status_detail(
                    stats,
                    parent_detail,
                    action="skipped",
                    reason=f"parent_offer_status_not_updated:{str(exc)}",
                )
            except Exception as exc:  # noqa: BLE001
                error_reason = str(exc)
                if _marketplace_status_update_error_is_safe_noop(
                    marketplace=marketplace,
                    reason=error_reason,
                ):
                    _status_detail(
                        stats,
                        parent_detail,
                        action="skipped",
                        reason=f"parent_offer_status_unchanged:{error_reason}",
                    )
                else:
                    _error_detail(
                        stats,
                        parent_detail,
                        reason=f"parent_offer_status_update_failed:{error_reason}",
                    )
            else:
                parent_pending, parent_task_id = _is_pending_response(
                    parent_response if isinstance(parent_response, dict) else None
                )
                if parent_pending:
                    parent_reason = "parent_offer_status_async_task_pending"
                    if parent_task_id:
                        parent_reason = f"{parent_reason}:{parent_task_id}"
                    _status_detail(
                        stats,
                        parent_detail,
                        action="skipped",
                        reason=parent_reason,
                    )
                else:
                    _status_detail(
                        stats,
                        parent_detail,
                        action="parent_status_updated",
                        enabled=parent_enabled,
                    )

            offer.is_enabled = parent_enabled
            offer.last_synced_at = now
            db.add(offer)
            continue

        target_values = {item[2] for item in decisions}
        if len(target_values) > 1:
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                _skip_detail(stats, detail, reason=f"mixed_offer_stock_requires_variant_control:{reason}")
            continue

        target_enabled = decisions[0][2]
        try:
            response = client.set_offer_enabled(offer.marketplace_offer_id, target_enabled)
        except NotImplementedError as exc:
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                if target_enabled:
                    _skip_detail(stats, detail, reason=f"{str(exc)}:{reason}")
                else:
                    _error_detail(
                        stats,
                        detail,
                        reason=f"marketplace_disable_not_supported:{str(exc)}:{reason}",
                    )
            continue
        except Exception as exc:  # noqa: BLE001
            error_reason = str(exc)
            status_safe_noop = _marketplace_status_update_error_is_safe_noop(
                marketplace=marketplace,
                reason=error_reason,
            )
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                if status_safe_noop:
                    _skip_detail(
                        stats,
                        detail,
                        reason=f"offer_status_unchanged:{error_reason}:{reason}",
                    )
                else:
                    _error_detail(stats, detail, reason=f"{error_reason}:{reason}")
            if status_safe_noop:
                offer.is_enabled = target_enabled
                offer.last_synced_at = now
                db.add(offer)
            continue

        is_pending, task_id = _is_pending_response(response if isinstance(response, dict) else None)
        if is_pending:
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                pending_reason = "marketplace_async_task_pending"
                if task_id:
                    pending_reason = f"{pending_reason}:{task_id}"
                _skip_detail(stats, detail, reason=f"{pending_reason}:{reason}")
            continue

        offer.is_enabled = target_enabled
        offer.last_synced_at = now
        db.add(offer)
        for mapping, supplier, _in_stock, reason, required_balance in decisions:
            detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
            detail["required_balance_usd"] = str(required_balance)
            detail["stock_reason"] = reason
            _updated_detail(stats, detail, enabled=target_enabled)

    for group in grouped.values():
        offer = group[0].marketplace_offer
        if offer is None:
            continue
        marketplace = offer.marketplace.lower()
        client = marketplace_clients.get(marketplace)

        managed = [
            mapping for mapping in group
            if mapping.manage_stock_quantity and mapping.supplier_product is not None
        ]
        if not managed:
            continue
        if offer.manually_disabled:
            for mapping in managed:
                detail = _base_detail(
                    mapping=mapping,
                    offer=offer,
                    supplier=mapping.supplier_product,
                )
                _skip_detail(stats, detail, reason="manual_halt_stock_quantity_skipped")
            continue
        if client is None or not hasattr(client, "set_offer_stock"):
            for mapping in managed:
                detail = _base_detail(
                    mapping=mapping,
                    offer=offer,
                    supplier=mapping.supplier_product,
                )
                _skip_detail(
                    stats,
                    detail,
                    reason="stock_quantity_update_not_supported_by_marketplace",
                )
            continue

        mapping = managed[0]
        supplier = mapping.supplier_product
        # Refresh cost_usd from the supplier API for variable recharge products
        # (service_id format: "recharge:{id}" with no denomination suffix).
        supplier_id_parts = (supplier.supplier_product_id or "").split(":")
        if (
            len(supplier_id_parts) == 2
            and supplier_id_parts[0] == "recharge"
            and hasattr(supplier_balance_client, "get_recharge_service_rate")
        ):
            try:
                fresh_rate = supplier_balance_client.get_recharge_service_rate(supplier_id_parts[1])
            except Exception:  # noqa: BLE001
                fresh_rate = None
            if fresh_rate is not None and fresh_rate > 0:
                supplier.cost_usd = fresh_rate
                db.add(supplier)
                db.commit()
        stock_qty = _compute_managed_stock_quantity(
            usable_balance_usd=usable_balance_usd,
            cost_per_unit_usd=supplier.cost_usd,
            min_purchase_quantity=mapping.min_purchase_quantity,
        )
        stock_detail: dict[str, Any] = {
            "offer_id": offer.id,
            "marketplace": offer.marketplace,
            "marketplace_offer_id": offer.marketplace_offer_id,
            "computed_stock": stock_qty,
            "cost_per_unit_usd": str(supplier.cost_usd),
            "usable_balance_usd": str(usable_balance_usd),
            "min_purchase_quantity": mapping.min_purchase_quantity,
        }
        if stock_qty == 0:
            # Marketplaces disallow zero stock; disable the listing instead.
            stock_detail["action"] = "stock_zero_listing_disabled"
            if hasattr(client, "set_offer_enabled"):
                try:
                    client.set_offer_enabled(offer.marketplace_offer_id, False)
                    offer.is_enabled = False
                    offer.last_synced_at = now
                    db.add(offer)
                    stock_detail["status"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    stock_detail["status"] = "error"
                    stock_detail["reason"] = str(exc)
                    stats["errors"] += 1
            else:
                stock_detail["status"] = "skipped"
                stock_detail["reason"] = "no_disable_api"
            stats["details"].append(stock_detail)
            continue
        stock_detail["action"] = "stock_quantity_update"
        try:
            cast(Any, client).set_offer_stock(
                offer.marketplace_offer_id,
                stock_qty,
                min_purchase_quantity=mapping.min_purchase_quantity,
            )
            stock_detail["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            stock_detail["status"] = "error"
            stock_detail["reason"] = str(exc)
            stats["errors"] += 1
        stats["details"].append(stock_detail)

    if stats["errors"] > 0:
        stats["status"] = "partial"

    return stats


def _build_marketplace_clients(settings: Settings) -> dict[str, MarketplaceStockClientProtocol]:
    clients: dict[str, MarketplaceStockClientProtocol] = {}
    if settings.digiseller_api_key:
        clients["digiseller"] = DigisellerClient(
            settings.digiseller_base_url,
            settings.digiseller_api_key,
            seller_id=settings.digiseller_seller_id,
            task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
            task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
        )
    if settings.ggsel_api_key:
        clients["ggsel"] = GGSELClient(
            settings.ggsel_base_url,
            settings.ggsel_api_key,
            seller_id=settings.ggsel_seller_id,
            panel_email=settings.ggsel_email,
            panel_password=settings.ggsel_password,
        )
    return clients


def run_stock_sync(
    *,
    force_marketplace_offer_ids: set[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    sync_run: SyncRun | None = None

    if not settings.gengine_api_key:
        log_event(
            logger,
            "stock_sync.skipped",
            reason="gengine_api_key_missing",
            offers_enabled=0,
            offers_disabled=0,
        )
        return {
            "status": "skipped",
            "reason": "GENGINE_API_KEY is required for stock sync.",
            "offers_enabled": 0,
            "offers_disabled": 0,
            "skipped": 0,
            "errors": 0,
        }

    supplier_client: GEngineClient | None = None
    marketplace_clients: dict[str, MarketplaceStockClientProtocol] = {}
    try:
        supplier_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
        marketplace_clients = _build_marketplace_clients(settings)
        with SessionLocal() as db:
            sync_run = SyncRun(job_name="stock_sync", status="running")
            db.add(sync_run)
            db.commit()

            log_event(logger, "stock_sync.started")
            stats = sync_offer_stock(
                db=db,
                settings=settings,
                supplier_balance_client=supplier_client,
                marketplace_clients=marketplace_clients,
                force_marketplace_offer_ids=force_marketplace_offer_ids,
            )

            sync_run.status = "success" if stats["status"] == "ok" else str(stats["status"])
            sync_run.finished_at = datetime.now(timezone.utc)
            sync_run.details_json = json.dumps(stats, ensure_ascii=True, separators=(",", ":"))
            db.add(sync_run)
            db.commit()

            log_event(
                logger,
                "stock_sync.finished",
                offers_checked=stats["offers_checked"],
                offers_enabled=stats["offers_enabled"],
                offers_disabled=stats["offers_disabled"],
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
        log_event(logger, "stock_sync.failed", error=str(exc))
        raise
    finally:
        for client in marketplace_clients.values():
            client.close()
        if supplier_client is not None:
            supplier_client.close()
