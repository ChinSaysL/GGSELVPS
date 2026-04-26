from __future__ import annotations

import json
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


def _marketplace_disable_error_is_safe_noop(*, marketplace: str, reason: str) -> bool:
    if marketplace != "digiseller":
        return False
    return _digiseller_disable_is_already_unavailable_error(reason)


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
    db.flush()

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

    mappings = _load_mappings(db)
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
                supplier_is_available=supplier.is_available,
                supplier_stock=supplier.supplier_stock,
                usable_balance_usd=usable_balance_usd,
                supplier_cost_usd=supplier.cost_usd,
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

        has_variant_bindings = any(item[0].marketplace_variant_id is not None for item in decisions)
        if marketplace == "digiseller" and has_variant_bindings:
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
                    if _marketplace_disable_error_is_safe_noop(
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
                variant_id = mapping.marketplace_variant_id
                if variant_id is None:
                    variant_control_failed = True
                    _skip_detail(stats, detail, reason="variant_id_missing_for_variant_offer")
                    continue

                desired_default: bool | None = None
                if default_variant_id is not None:
                    desired_default = int(variant_id) == default_variant_id

                try:
                    response = _set_offer_variant_stock_state(
                        client=variant_client,
                        offer_id=offer.marketplace_offer_id,
                        variant_id=int(variant_id),
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
                if not parent_enabled and _marketplace_disable_error_is_safe_noop(
                    marketplace=marketplace,
                    reason=error_reason,
                ):
                    _status_detail(
                        stats,
                        parent_detail,
                        action="skipped",
                        reason=f"parent_offer_already_unavailable:{error_reason}",
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
                _skip_detail(stats, detail, reason=f"{str(exc)}:{reason}")
            continue
        except Exception as exc:  # noqa: BLE001
            error_reason = str(exc)
            disable_safe_noop = (
                not target_enabled
                and _marketplace_disable_error_is_safe_noop(
                    marketplace=marketplace,
                    reason=error_reason,
                )
            )
            for mapping, supplier, _in_stock, reason, required_balance in decisions:
                detail = _base_detail(mapping=mapping, offer=offer, supplier=supplier)
                detail["required_balance_usd"] = str(required_balance)
                if disable_safe_noop:
                    _skip_detail(
                        stats,
                        detail,
                        reason=f"offer_already_unavailable:{error_reason}:{reason}",
                    )
                else:
                    _error_detail(stats, detail, reason=f"{error_reason}:{reason}")
            if disable_safe_noop:
                offer.is_enabled = False
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
        clients["ggsel"] = GGSELClient(settings.ggsel_base_url, settings.ggsel_api_key)
    return clients


def run_stock_sync() -> dict[str, Any]:
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
            db.flush()

            log_event(logger, "stock_sync.started")
            stats = sync_offer_stock(
                db=db,
                settings=settings,
                supplier_balance_client=supplier_client,
                marketplace_clients=marketplace_clients,
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
