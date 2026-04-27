from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class MenuAction:
    key: str
    label: str
    command: str
    description: str


MENU_ACTIONS: tuple[MenuAction, ...] = (
    MenuAction("1", "Check config", "check-config", "Show safe .env and runtime status."),
    MenuAction("2", "Initialize DB", "init-db", "Create SQLite tables if they do not exist."),
    MenuAction("3", "Run tests", "test", "Run pytest."),
    MenuAction("4", "Run lint", "lint", "Run Ruff."),
    MenuAction("5", "Run type check", "typecheck", "Run mypy against app/."),
    MenuAction("6", "Run all checks", "check", "Run tests, Ruff, and mypy."),
    MenuAction("7", "Start API", "run-api", "Start uvicorn on 0.0.0.0:8000."),
    MenuAction("8", "Sync products", "sync-products", "Import products from G-Engine."),
    MenuAction("9", "List products", "list-products", "Show imported supplier products."),
    MenuAction("10", "List offers", "list-offers", "Show manually linked marketplace offers."),
    MenuAction("11", "Manual link offer", "manual-link", "Link a marketplace offer interactively."),
    MenuAction("12", "Auto-link offers", "auto-link", "Auto-link marketplace offers to G-Engine sub-items."),
    MenuAction(
        "13",
        "Preview prices (dry-run)",
        "price-preview",
        "Show recommended prices for linked offers without updating marketplaces.",
    ),
    MenuAction("14", "Sync prices (live)", "sync-prices", "Update linked marketplace offer prices."),
    MenuAction(
        "15",
        "Sync stock (live)",
        "sync-stock",
        "Apply stock/availability updates from supplier stock and usable balance.",
    ),
    MenuAction("16", "Sync orders (live)", "sync-orders", "Process paid orders and delivery."),
    MenuAction("17", "Manual link help", "manual-link-example", "Print an example link request."),
    MenuAction("18", "Docker build", "docker-build", "Run docker compose build."),
)

VALID_MARKETPLACES = ("digiseller", "ggsel")
VALID_CURRENCIES = ("RUB", "USD")
GENGINE_LINK_KINDS = ("shop", "recharge", "gifts")


@dataclass(frozen=True)
class GEngineProductReference:
    kind: str
    product_id: str
    slug: str | None
    source: str


@dataclass
class _ListSupplierClient:
    products: list[dict[str, Any]]

    def list_products(self) -> list[dict[str, Any]]:
        return self.products

    def close(self) -> None:
        return None


def _run_command(command: Sequence[str]) -> int:
    print(f"\n$ {' '.join(command)}\n")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _run_python_module(module: str, *args: str) -> int:
    return _run_command((sys.executable, "-m", module, *args))


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _safe_yes_no(value: bool) -> str:
    return "yes" if value else "no"


def action_check_config() -> int:
    _print_header("Config")
    try:
        from app.config import get_settings
        from app.security import mask_secret
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    settings = get_settings()
    env_path = REPO_ROOT / ".env"
    data_dir = REPO_ROOT / "data"

    print(f".env exists: {_safe_yes_no(env_path.exists())}")
    print(f"data directory exists: {_safe_yes_no(data_dir.exists())}")
    print(f"database_url: {settings.database_url}")
    print(f"scheduler_enabled: {settings.scheduler_enabled}")
    print(f"price_sync_interval_minutes: {settings.price_sync_interval_minutes}")
    print(f"pricing_mode: {settings.pricing_mode}")
    print(
        "digiseller_task_poll_timeout_seconds: "
        f"{settings.digiseller_task_poll_timeout_seconds}"
    )
    print(
        "digiseller_task_poll_interval_seconds: "
        f"{settings.digiseller_task_poll_interval_seconds}"
    )
    print(
        "ggsel_default_marketplace_commission_percent: "
        f"{settings.ggsel_default_marketplace_commission_percent}"
    )
    print(
        "ggsel_default_payment_system_commission_percent: "
        f"{settings.ggsel_default_payment_system_commission_percent}"
    )
    print(f"default_currency: {settings.default_currency}")
    print(f"manual_usd_rub_rate configured: {_safe_yes_no(settings.manual_usd_rub_rate is not None)}")
    print(f"gengine_api_key: {mask_secret(settings.gengine_api_key)}")
    print(f"ggsel_api_key: {mask_secret(settings.ggsel_api_key)}")
    print(f"ggsel_seller_id configured: {_safe_yes_no(bool(settings.ggsel_seller_id))}")
    print(f"digiseller_api_key: {mask_secret(settings.digiseller_api_key)}")
    print(f"digiseller_seller_id configured: {_safe_yes_no(bool(settings.digiseller_seller_id))}")

    missing: list[str] = []
    warnings: list[str] = []
    if not settings.gengine_api_key:
        missing.append("GENGINE_API_KEY")
    if not settings.scheduler_enabled:
        warnings.append(
            "SCHEDULER_ENABLED is false, so automatic hourly price sync is disabled."
        )
    if settings.digiseller_api_key and not settings.digiseller_seller_id:
        warnings.append(
            "DIGISELLER_SELLER_ID is not set. Digiseller live price updates may return 401 "
            "after token expiry; set seller ID to enable automatic token refresh."
        )
    if settings.ggsel_api_key and not settings.ggsel_seller_id:
        warnings.append(
            "GGSEL_SELLER_ID is not set. GGSEL public offer lookup can still work, but live "
            "price/status writes may return 401 unless GGSEL_API_KEY is already a short-lived "
            "seller API token."
        )
    if settings.default_currency == "RUB":
        if settings.manual_usd_rub_rate is None and not settings.auto_usd_rub_rate:
            warnings.append(
                "Set MANUAL_USD_RUB_RATE or enable AUTO_USD_RUB_RATE for RUB suggestions/sync."
            )

    if missing:
        print("\nMissing before product import testing:")
        for item in missing:
            print(f"- {item}")
        return 1

    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"- {item}")

    print("\nConfig looks usable for product import testing.")
    return 0


def action_init_db() -> int:
    _print_header("Initialize Database")
    try:
        from app.db import init_db
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    init_db()
    print("Database tables are ready.")
    return 0


def action_test() -> int:
    return _run_python_module("pytest")


def action_lint() -> int:
    return _run_python_module("ruff", "check", ".")


def action_typecheck() -> int:
    return _run_python_module("mypy", "app")


def action_check() -> int:
    for action in (action_test, action_lint, action_typecheck):
        code = action()
        if code != 0:
            return code
    return 0


def action_run_api() -> int:
    return _run_python_module("uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000")


def action_sync_products() -> int:
    _print_header("Product Sync")
    try:
        from app.services.product_sync import run_product_sync
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    try:
        result = run_product_sync()
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Product sync failed: {exc}")
        return 1

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


def _print_price_sync_result(result: dict[str, Any]) -> None:
    summary_keys = (
        "status",
        "dry_run",
        "offers_checked",
        "prices_previewed",
        "prices_updated",
        "skipped",
        "errors",
        "supplier_refresh_products_checked",
        "supplier_products_refreshed",
        "supplier_refresh_errors",
    )
    for key in summary_keys:
        if key in result:
            print(f"{key}: {result[key]}")

    details = result.get("details")
    if not isinstance(details, list) or not details:
        return

    print("\nDetails:")
    for item in details[:25]:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        marketplace = item.get("marketplace")
        marketplace_offer_id = item.get("marketplace_offer_id")
        title = item.get("title")
        denomination = item.get("denomination")
        old_price = item.get("old_price")
        recommended_price = item.get("recommended_price")
        currency = item.get("currency")
        reason = item.get("reason")

        line = (
            f"- {action}: {marketplace}:{marketplace_offer_id} | {title} "
            f"{denomination or ''} | {old_price} -> {recommended_price} {currency or ''}"
        )
        if reason:
            line += f" | reason={reason}"
        print(line)

    if len(details) > 25:
        print(f"...and {len(details) - 25} more details.")


def action_price_preview() -> int:
    _print_header("Price Preview")
    print(
        "This refreshes linked G-Engine costs when possible, then shows cost-based "
        "recommended prices. It does not update marketplace listings."
    )
    try:
        from app.services.price_sync import run_price_sync
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    try:
        result = run_price_sync(dry_run=True)
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Price preview failed: {exc}")
        return 1

    _print_price_sync_result(result)
    return 0 if result.get("errors", 0) == 0 else 1


def action_sync_prices() -> int:
    _print_header("Price Sync (Manual Trigger)")
    print(
        "This triggers price sync immediately. For automatic hourly sync, keep "
        "SCHEDULER_ENABLED=true and PRICE_SYNC_INTERVAL_MINUTES=60."
    )
    if sys.stdin.isatty() and not _ask_bool("Update marketplace prices now (live mode)?", default=True):
        print("Cancelled.")
        return 0

    try:
        from app.services.price_sync import run_price_sync
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    try:
        result = run_price_sync(dry_run=False)
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Price sync failed: {exc}")
        return 1

    _print_price_sync_result(result)
    return 0 if result.get("errors", 0) == 0 else 1


def action_sync_stock() -> int:
    _print_header("Stock Sync (Manual Trigger)")
    print(
        "This checks supplier stock and usable balance, then applies stock visibility "
        "updates for linked offers."
    )
    if sys.stdin.isatty() and not _ask_bool("Update stock status now (live mode)?", default=True):
        print("Cancelled.")
        return 0

    try:
        from app.services.stock_sync import run_stock_sync
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    try:
        result = run_stock_sync()
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Stock sync failed: {exc}")
        return 1

    _print_stock_sync_result(result)
    return 0 if result.get("errors", 0) == 0 else 1


def action_sync_orders() -> int:
    _print_header("Order Sync (Manual Trigger)")
    print("This checks paid marketplace orders, buys linked G-Engine items, and delivers them.")
    if sys.stdin.isatty() and not _ask_bool("Process paid orders now (live mode)?", default=True):
        print("Cancelled.")
        return 0

    try:
        from app.services.order_sync import run_order_sync
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    try:
        result = run_order_sync()
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Order sync failed: {exc}")
        return 1

    _print_order_sync_result(result)
    return 0 if result.get("errors", 0) == 0 else 1


def _print_order_sync_result(result: dict[str, Any]) -> None:
    for key in (
        "status",
        "orders_detected",
        "orders_processed",
        "supplier_orders_created",
        "orders_delivered",
        "skipped",
        "errors",
    ):
        if key in result:
            print(f"{key}: {result[key]}")

    details = result.get("details")
    if isinstance(details, list):
        notable = [
            item
            for item in details
            if isinstance(item, dict) and item.get("action") in {"skipped", "error"}
        ]
        if notable:
            print("Notable details (first 10):")
            for item in notable[:10]:
                print(
                    f"- order={item.get('marketplace_order_id') or item.get('order_id')} | "
                    f"reason={item.get('reason')}"
                )


def _print_stock_sync_result(result: dict[str, Any]) -> None:
    for key in (
        "status",
        "balance_usd",
        "usable_balance_usd",
        "offers_checked",
        "offers_enabled",
        "offers_disabled",
        "skipped",
        "errors",
    ):
        if key in result:
            print(f"{key}: {result[key]}")

    details = result.get("details")
    if isinstance(details, list):
        skip_details = [item for item in details if isinstance(item, dict) and item.get("action") == "skipped"]
        if skip_details:
            print("Skipped reasons (first 10):")
            for item in skip_details[:10]:
                print(
                    f"- {item.get('marketplace')}:{item.get('marketplace_offer_id')} | "
                    f"mapping={item.get('mapping_id')} | reason={item.get('reason')}"
                )
        error_details = [item for item in details if isinstance(item, dict) and item.get("action") == "error"]
        if error_details:
            print("Error reasons (first 10):")
            for item in error_details[:10]:
                print(
                    f"- {item.get('marketplace')}:{item.get('marketplace_offer_id')} | "
                    f"mapping={item.get('mapping_id')} | reason={item.get('reason')}"
                )


def _format_money(value: object) -> str:
    return "" if value is None else str(value)


def _product_label(product: Any) -> str:
    return (
        f"[local {product.id}] {product.title} | denomination={product.denomination or ''} | "
        f"category={product.category or ''} | supplier_id={product.supplier_product_id} | "
        f"cost_usd={_format_money(product.cost_usd)} | stock={product.supplier_stock}"
    )


def _product_search_blob(product: Any) -> str:
    parts = (
        getattr(product, "id", None),
        getattr(product, "title", None),
        getattr(product, "category", None),
        getattr(product, "denomination", None),
        getattr(product, "region", None),
        getattr(product, "supplier_product_id", None),
        getattr(product, "supplier_stock", None),
        getattr(product, "raw_payload_json", None),
    )
    return _normalize_search_text(" ".join("" if part is None else str(part) for part in parts))


def _normalize_search_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _title_from_slug(slug: str | None) -> str | None:
    if not slug:
        return None
    normalized = _normalize_search_text(slug)
    if not normalized:
        return None
    replacements = {
        "psn": "PSN",
        "ru": "RU",
        "cis": "CIS",
        "try": "TRY",
        "tl": "TL",
    }
    words = [replacements.get(word, word.capitalize()) for word in normalized.split()]
    return " ".join(words)


def _parse_gengine_reference(value: str) -> GEngineProductReference | None:
    raw_value = value.strip()
    if not raw_value:
        return None
    if raw_value.isdigit():
        return GEngineProductReference(
            kind="unknown",
            product_id=raw_value,
            slug=None,
            source=raw_value,
        )

    parsed = urlparse(raw_value)
    path = parsed.path if parsed.scheme or parsed.netloc else raw_value
    segments = [unquote(segment).strip() for segment in path.split("/") if segment.strip()]
    normalized_segments = [segment.lower() for segment in segments]

    for kind in GENGINE_LINK_KINDS:
        if kind not in normalized_segments:
            continue
        index = normalized_segments.index(kind)
        if len(segments) <= index + 1:
            return None
        product_id = segments[index + 1].strip()
        if not product_id.isdigit():
            return None
        slug = segments[index + 2].strip() if len(segments) > index + 2 else None
        return GEngineProductReference(
            kind=kind,
            product_id=product_id,
            slug=slug or None,
            source=raw_value,
        )

    return None


def _raw_payload_dict(product: Any) -> dict[str, Any]:
    raw_payload = getattr(product, "raw_payload_json", None)
    if not raw_payload:
        return {}
    try:
        payload = json.loads(str(raw_payload))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_payload_values(value: object, keys: set[str]) -> set[str]:
    collected: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in keys and item not in {None, ""}:
                collected.add(str(item).strip())
            collected.update(_collect_payload_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            collected.update(_collect_payload_values(item, keys))
    return collected


def _product_reference_ids(product: Any) -> set[str]:
    payload = _raw_payload_dict(product)
    id_keys = {
        "id",
        "product_id",
        "productid",
        "parent_product_id",
        "base_product_id",
        "shop_product_id",
        "recharge_product_id",
        "gift_id",
        "item_id",
        "sku",
    }
    values = {
        str(value).strip()
        for value in (
            getattr(product, "supplier_product_id", None),
        )
        if value not in {None, ""}
    }
    values.update(_collect_payload_values(payload, id_keys))
    return {value for value in values if value}


def _product_direct_supplier_id(product: Any) -> str:
    value = getattr(product, "supplier_product_id", None)
    return "" if value in {None, ""} else str(value).strip()


def _product_payload_id(product: Any, key: str) -> str | None:
    payload = _raw_payload_dict(product)
    value = payload.get(key)
    if value in {None, ""}:
        return None
    return str(value).strip()


def _product_reference_parent_id(product: Any, reference: GEngineProductReference) -> str | None:
    if reference.kind == "recharge":
        return _product_payload_id(product, "recharge_product_id")
    if reference.kind == "shop":
        return (
            _product_payload_id(product, "base_product_id")
            or _product_payload_id(product, "shop_product_id")
            or _product_payload_id(product, "product_id")
        )
    if reference.kind == "gifts":
        return _product_payload_id(product, "gift_id")
    return None


def _is_specific_reference_product(product: Any, reference: GEngineProductReference) -> bool:
    direct_supplier_id = _product_direct_supplier_id(product)
    if not direct_supplier_id:
        return False
    if direct_supplier_id == reference.product_id:
        return False
    return _product_reference_parent_id(product, reference) == reference.product_id


def _product_matches_reference(product: Any, reference: GEngineProductReference) -> bool:
    if reference.product_id in _product_reference_ids(product):
        return True
    if reference.slug is None:
        return False
    return _normalize_search_text(reference.slug) in _product_search_blob(product)


def _product_matches_reference_id(product: Any, reference: GEngineProductReference) -> bool:
    return reference.product_id in _product_reference_ids(product)


def _product_matches_reference_slug(product: Any, reference: GEngineProductReference) -> bool:
    if reference.slug is None:
        return False
    return _normalize_search_text(reference.slug) in _product_search_blob(product)


def _sort_products_for_reference(
    products: list[Any],
    reference: GEngineProductReference,
) -> list[Any]:
    def score(product: Any) -> tuple[int, int]:
        ids = _product_reference_ids(product)
        direct_id_score = 0 if reference.product_id in ids else 1
        return (direct_id_score, product.id)

    return sorted(products, key=score)


def _import_gengine_reference(reference: GEngineProductReference) -> int:
    if reference.kind not in {"shop", "unknown"}:
        print(
            f"On-demand import for G-Engine '{reference.kind}' links is not implemented yet. "
            "Run the normal product sync, or use a local product row already imported."
        )
        return 0

    try:
        from app.clients import GEngineClient
        from app.config import get_settings
        from app.db import SessionLocal
        from app.services.product_sync import sync_products
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 0

    settings = get_settings()
    client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
    try:
        products = client.list_product_denominations(
            reference.product_id,
            fallback_title=_title_from_slug(reference.slug),
            source_slug=reference.slug,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Could not fetch G-Engine product ID {reference.product_id}: {exc}")
        return 0
    finally:
        client.close()

    if not products:
        print(f"G-Engine returned no denominations for product ID {reference.product_id}.")
        return 0

    with SessionLocal() as db:
        result = sync_products(
            db=db,
            settings=settings,
            supplier_client=_ListSupplierClient(products),
            marketplace_name=None,
        )
        db.commit()

    return int(result["products_imported"])


def _load_recent_products(limit: int = 200) -> list[Any]:
    try:
        from sqlalchemy import desc, select

        from app.db import SessionLocal
        from app.models import SupplierProduct
    except ImportError as exc:
        raise RuntimeError(
            f"Could not import app dependencies: {exc}\n"
            "Run: python -m pip install -r requirements.txt"
        ) from exc

    with SessionLocal() as db:
        return list(
            db.execute(select(SupplierProduct).order_by(desc(SupplierProduct.updated_at)).limit(limit))
            .scalars()
            .all()
        )


def action_list_products() -> int:
    _print_header("Imported Products")
    try:
        products = _load_recent_products(limit=25)
    except RuntimeError as exc:
        print(exc)
        return 1

    if not products:
        print("No imported products yet. Run product sync first.")
        return 0

    for product in products:
        print(_product_label(product))
    return 0


def action_list_offers() -> int:
    _print_header("Linked Offers")
    try:
        from sqlalchemy import desc, select

        from app.db import SessionLocal
        from app.models import MarketplaceOffer
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    with SessionLocal() as db:
        offers = (
            db.execute(select(MarketplaceOffer).order_by(desc(MarketplaceOffer.updated_at)).limit(25))
            .scalars()
            .all()
        )

    if not offers:
        print("No linked offers yet. Create an offer manually, then use /offers/manual-link.")
        return 0

    for offer in offers:
        print(
            f"[{offer.id}] {offer.marketplace}:{offer.marketplace_offer_id} | "
            f"supplier_product_id={offer.supplier_product_id} | "
            f"price={_format_money(offer.price)} {offer.currency} | enabled={offer.is_enabled} | "
            f"buyer_input={offer.requires_buyer_input}"
        )
    return 0


def action_manual_link_example() -> int:
    _print_header("Manual Link Example")
    print(
        """Create the marketplace offer manually first, then link it:

curl -X POST http://127.0.0.1:8000/offers/manual-link \\
  -H "Content-Type: application/json" \\
  -d "{\\"marketplace\\":\\"digiseller\\",\\"marketplace_offer_id\\":\\"123456\\",\\"supplier_product_id\\":1,\\"title\\":\\"Steam Gift Card 10 USD\\",\\"price\\":\\"999.00\\",\\"currency\\":\\"RUB\\",\\"offer_enabled\\":false,\\"requires_buyer_input\\":true,\\"buyer_input_label\\":\\"Steam friend code\\",\\"buyer_input_help\\":\\"Ask the buyer to provide their Steam friend code before fulfillment.\\",\\"mapping_enabled\\":true,\\"marketplace_variant_id\\":42,\\"marketplace_variant_name\\":\\"250 TRY\\"}"
"""
    )
    return 0


def _ask_text(prompt: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default not in {None, ""} else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("This value is required.")


def _ask_bool(prompt: str, *, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "true", "1", "on"}:
            return True
        if value in {"n", "no", "false", "0", "off"}:
            return False
        print("Please enter yes or no.")


def _ask_decimal(prompt: str, *, default: Decimal | None = None) -> Decimal:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            print("Please enter a valid number, for example 999.00.")
            continue
        if decimal_value <= 0:
            print("Price must be greater than 0.")
            continue
        return decimal_value


def _ask_optional_percent_as_ratio(prompt: str, *, default_percent: Decimal | None = None) -> Decimal | None:
    while True:
        suffix = f" [{default_percent}]" if default_percent is not None else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value:
            if default_percent is None:
                return None
            return default_percent / Decimal("100")
        try:
            percent_value = Decimal(value)
        except InvalidOperation:
            print("Please enter a valid percent number, for example 22 or 15.5.")
            continue
        if percent_value < 0:
            print("Percent must be >= 0.")
            continue
        if percent_value < Decimal("1"):
            print(f"Interpreting {percent_value} as a ratio, i.e. {(percent_value * Decimal('100'))}%")
            return percent_value
        return percent_value / Decimal("100")


def _default_offer_title(product: Any) -> str:
    denomination = str(product.denomination or "").strip()
    title = str(product.title).strip()
    if denomination and denomination.lower() not in title.lower():
        return f"{title} - {denomination}"
    return title


def _default_buyer_input_label(product: Any) -> str:
    text = f"{product.title} {product.category or ''} {product.denomination or ''}".lower()
    if "steam" in text:
        return "Steam friend code"
    return "Buyer input"


def _default_buyer_input_help(label: str) -> str:
    return f"Ask the buyer to provide their {label} before fulfillment."


def _suggest_price(
    product: Any,
    currency: str,
    *,
    marketplace: str,
    markup_percent: Decimal | None = None,
    marketplace_commission_percent: Decimal | None = None,
    payment_system_commission_percent: Decimal | None = None,
) -> Decimal | None:
    try:
        from app.pricing import calculate_final_price_usd, convert_usd_to_currency
        from app.services.fx import resolve_usd_rub_rate
        from app.config import get_settings
    except ImportError:
        return None

    settings = get_settings()
    calculation = calculate_final_price_usd(
        product.cost_usd,
        pricing_mode=settings.pricing_mode,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
        markup_percent=markup_percent,
    )
    desired_net = calculation.final_price_usd
    if marketplace == "ggsel":
        ggsel_marketplace_commission = (
            marketplace_commission_percent
            if marketplace_commission_percent is not None
            else Decimal(str(settings.ggsel_default_marketplace_commission_percent))
        )
        ggsel_payment_system_commission = (
            payment_system_commission_percent
            if payment_system_commission_percent is not None
            else Decimal(str(settings.ggsel_default_payment_system_commission_percent))
        )
        total_commission = ggsel_marketplace_commission + ggsel_payment_system_commission
        if total_commission >= Decimal("1"):
            return None
        desired_net = (desired_net / (Decimal("1") - total_commission)).quantize(
            Decimal("0.01"),
            rounding=ROUND_UP,
        )

    if currency == "USD":
        return desired_net
    if currency == "RUB":
        try:
            usd_rub_rate = resolve_usd_rub_rate(settings)
        except Exception:
            return None
        return convert_usd_to_currency(
            desired_net,
            "RUB",
            manual_usd_rub_rate=usd_rub_rate,
        )
    return None


def _payload_first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _payload_first_bool(payload: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _payload_first_decimal(payload: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


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
    matched = re.search(r"\d+", text)
    if matched is None:
        return None
    return int(matched.group(0))


def _extract_option_variant_items(option: dict[str, Any]) -> list[dict[str, Any]]:
    variant_containers = ("variants", "values", "items", "options")
    variant_value_keys = ("text", "label", "name", "title")
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for container_key in variant_containers:
        container = option.get(container_key)
        if not isinstance(container, list):
            continue
        for item in container:
            text: str | None = None
            variant_id: int | None = None
            is_default: bool = False
            if isinstance(item, dict):
                text = _payload_first_str(item, variant_value_keys)
                if not text:
                    fallback_text = _payload_first_str(item, ("value",))
                    if fallback_text and _parse_int_token(fallback_text) is None:
                        text = fallback_text
                raw_variant_id = _payload_first_str(
                    item,
                    (
                        "variant_id",
                        "variantId",
                        "id_variant",
                        "id_var",
                        "var_id",
                        "value_id",
                        "value",
                        "id_option_value",
                        "option_value_id",
                        "id",
                    ),
                )
                variant_id = _parse_int_token(raw_variant_id)
                if variant_id is None:
                    for key, raw_value in item.items():
                        if "id" not in str(key).lower():
                            continue
                        variant_id = _parse_int_token(raw_value)
                        if variant_id is not None:
                            break
                is_default = bool(_payload_first_bool(item, ("default", "is_default")))
            elif item is not None:
                text = str(item).strip()
            if not text:
                continue
            normalized = _normalize_search_text(text)
            key = (str(variant_id or ""), normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "variant_value": text,
                    "variant_id": variant_id,
                    "is_default": is_default,
                }
            )
        if items:
            break

    return items


def _extract_option_variant_values(option: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in _extract_option_variant_items(option):
        variant_value = str(item.get("variant_value") or "").strip()
        if variant_value:
            values.append(variant_value)
    return values


def _extract_marketplace_snapshot_fields(payload: dict[str, Any]) -> dict[str, Any]:
    title = _payload_first_str(
        payload,
        (
            "title",
            "name",
            "name_goods",
            "product_name",
            "good_name",
            "goods_name",
            "info_goods",
            "caption",
        ),
    )
    denomination = _payload_first_str(payload, ("denomination", "nominal", "variant", "option"))
    currency = _payload_first_str(payload, ("currency", "currency_code", "valute", "curr"))
    price = _payload_first_decimal(payload, ("price", "cost", "amount", "sum"))
    is_enabled = _payload_first_bool(
        payload,
        ("enabled", "is_enabled", "active", "is_active", "visible", "in_stock"),
    )
    requires_buyer_input = _payload_first_bool(
        payload,
        ("requires_buyer_input", "need_user_data", "require_input"),
    )
    buyer_input_label = _payload_first_str(
        payload,
        ("buyer_input_label", "user_data_label", "input_label"),
    )
    buyer_input_help = _payload_first_str(
        payload,
        ("buyer_input_help", "user_data_help", "input_help"),
    )
    options = payload.get("options")
    option_variants: list[str] = []
    option_variant_entries: list[dict[str, str]] = []
    option_parameter_names: list[str] = []
    detected_requires_buyer_input = False
    detected_buyer_input_label: str | None = None
    seen_variant_entries: set[tuple[str, str, str]] = set()
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            option_label = _payload_first_str(option, ("label", "name"))
            if option_label:
                option_parameter_names.append(option_label)
            option_type = _payload_first_str(option, ("type",))
            required_flag = _payload_first_bool(option, ("required", "is_required"))
            normalized_type = str(option_type or "").strip().lower()
            if normalized_type in {"text", "textarea"} and required_flag is not False:
                detected_requires_buyer_input = True
                if detected_buyer_input_label is None:
                    detected_buyer_input_label = option_label
            variants = _extract_option_variant_items(option)
            default_variant_text: str | None = None
            raw_variants = option.get("variants")
            default_variant_keys = ("default", "is_default")
            if isinstance(raw_variants, list):
                for raw_variant in raw_variants:
                    if not isinstance(raw_variant, dict):
                        continue
                    variant_text = _payload_first_str(raw_variant, ("text", "label", "name", "value"))
                    if default_variant_text is None and _payload_first_bool(raw_variant, default_variant_keys):
                        default_variant_text = variant_text

            for variant_item in variants:
                variant_text = str(variant_item.get("variant_value") or "")
                variant_id = variant_item.get("variant_id")
                option_variants.append(variant_text)
                option_key = _normalize_search_text(option_label or "option")
                variant_key = _normalize_search_text(variant_text)
                variant_id_key = str(variant_id) if variant_id is not None else ""
                composite_key = (option_key, variant_key, variant_id_key)
                if composite_key in seen_variant_entries:
                    continue
                seen_variant_entries.add(composite_key)
                entry: dict[str, Any] = {
                    "option_label": option_label or "Option",
                    "variant_value": variant_text,
                }
                if isinstance(variant_id, int):
                    entry["variant_id"] = variant_id
                option_variant_entries.append(entry)

            if denomination is None and default_variant_text:
                denomination = default_variant_text
            if denomination is None and variants:
                first_value = str(variants[0].get("variant_value") or "")
                if first_value:
                    denomination = first_value
            if denomination is None and option_label:
                denomination = option_label

    return {
        "title": title,
        "denomination": denomination,
        "currency": currency.upper() if currency else None,
        "price": price,
        "is_enabled": is_enabled,
        "requires_buyer_input": requires_buyer_input,
        "buyer_input_label": buyer_input_label,
        "buyer_input_help": buyer_input_help,
        "option_variants": option_variants,
        "option_variant_entries": option_variant_entries,
        "option_parameter_names": option_parameter_names,
        "detected_requires_buyer_input": detected_requires_buyer_input,
        "detected_buyer_input_label": detected_buyer_input_label,
    }


def _extract_marketplace_offer_id(payload: dict[str, Any]) -> str | None:
    return _payload_first_str(
        payload,
        (
            "id",
            "product_id",
            "productId",
            "productID",
            "goods_id",
            "goodsId",
            "good_id",
            "goodId",
            "id_goods",
            "id_good",
            "id_product",
        ),
    )


def _fetch_marketplace_offer_snapshot(
    marketplace: str,
    marketplace_offer_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        from app.clients import DigisellerClient, GGSELClient
        from app.config import get_settings
    except ImportError:
        return None, "app dependencies are missing"

    settings = get_settings()
    client: Any | None = None
    try:
        if marketplace == "digiseller" and settings.digiseller_api_key:
            client = DigisellerClient(
                settings.digiseller_base_url,
                settings.digiseller_api_key,
                seller_id=settings.digiseller_seller_id,
                task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
                task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
            )
        elif marketplace == "ggsel" and settings.ggsel_api_key:
            client = GGSELClient(
                settings.ggsel_base_url,
                settings.ggsel_api_key,
                seller_id=settings.ggsel_seller_id,
            )
        else:
            return None, "marketplace API key is not configured"

        raw = client.get_offer_by_id(marketplace_offer_id)
        if not isinstance(raw, dict):
            return None, "marketplace API returned no item for this id"

        fields = _extract_marketplace_snapshot_fields(raw)
        fields["marketplace_offer_id"] = marketplace_offer_id
        fields["raw"] = raw
        return fields, None
    except Exception as exc:
        return None, str(exc)
    finally:
        if client is not None:
            client.close()


def _normalize_denom_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _snapshot_matches_selected_product(product: Any, snapshot: dict[str, Any]) -> bool:
    product_denom = _normalize_denom_text(getattr(product, "denomination", None))
    if not product_denom:
        return True

    snapshot_denom = _normalize_denom_text(snapshot.get("denomination"))
    snapshot_title = _normalize_denom_text(snapshot.get("title"))

    if snapshot_denom and product_denom in snapshot_denom:
        return True
    if snapshot_title and product_denom in snapshot_title:
        return True
    return False


def _variant_match_key(text: str | None) -> str:
    signature = _extract_denomination_signature(text)
    if signature:
        return signature
    return _normalize_search_text(text or "")


def _product_variant_key(product: Any) -> str:
    signature = _product_denomination_signature(product)
    if signature:
        return signature
    return _normalize_search_text(
        " ".join(
            [
                str(getattr(product, "denomination", "") or ""),
                str(getattr(product, "title", "") or ""),
                str(getattr(product, "category", "") or ""),
            ]
        )
    )


def _products_matching_variant(products: list[Any], variant_value: str) -> list[Any]:
    target_key = _variant_match_key(variant_value)
    if not target_key:
        return []

    strict_matches = [
        product
        for product in products
        if _product_variant_key(product) == target_key
    ]
    if strict_matches:
        return sorted(strict_matches, key=lambda item: int(item.id))

    normalized_variant = _normalize_search_text(variant_value)
    if not normalized_variant:
        return []

    fuzzy_matches = [
        product
        for product in products
        if normalized_variant in _product_search_blob(product)
    ]
    return sorted(fuzzy_matches, key=lambda item: int(item.id))


def _products_matching_variant_exact(products: list[Any], variant_value: str) -> list[Any]:
    target_key = _variant_match_key(variant_value)
    if not target_key:
        return []
    exact_matches = [
        product
        for product in products
        if _product_variant_key(product) == target_key
    ]
    return sorted(exact_matches, key=lambda item: int(item.id))


def _auto_match_option_entries_to_products(
    products: list[Any],
    option_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for entry in option_entries:
        option_label = str(entry.get("option_label") or "Option")
        variant_value = str(entry.get("variant_value") or "").strip()
        if not variant_value:
            continue
        found = _products_matching_variant_exact(products, variant_value)
        if len(found) == 1:
            matches.append(
                {
                    "entry": entry,
                    "product": found[0],
                    "option_description": f"{option_label}: {variant_value}",
                }
            )
            continue
        if not found:
            unmatched.append({"entry": entry})
            continue
        ambiguous.append(
            {
                "entry": entry,
                "products": found,
            }
        )

    return matches, unmatched, ambiguous


def _print_reference_products(products: list[Any], *, limit: int = 50) -> None:
    visible = products[:limit]
    for product in visible:
        print(_product_label(product))
    if len(products) > len(visible):
        print(f"...and {len(products) - len(visible)} more rows.")


def _choose_product_from_reference_rows(
    products: list[Any],
    *,
    prompt: str,
    default_product_id: int | None = None,
    allow_skip: bool = False,
) -> Any | None:
    by_id = {int(product.id): product for product in products}
    while True:
        default_text = str(default_product_id) if default_product_id is not None else None
        value = _ask_text(prompt, default=default_text, required=not allow_skip).strip()
        if not value and allow_skip:
            return None
        if not value.isdigit():
            print("Please enter a numeric local row id.")
            continue
        selected = by_id.get(int(value))
        if selected is None:
            print("That local row id is not in the current imported rows.")
            continue
        return selected


def _add_link_target(
    targets_by_product_id: dict[int, dict[str, Any]],
    *,
    product: Any,
    option_description: str | None,
    variant_id: int | None = None,
    variant_value: str | None = None,
) -> None:
    product_id = int(product.id)
    item = targets_by_product_id.setdefault(
        product_id,
        {"product": product, "option_descriptions": [], "variant_bindings": []},
    )
    if option_description and option_description not in item["option_descriptions"]:
        item["option_descriptions"].append(option_description)
    if variant_id is not None or variant_value:
        binding = {
            "variant_id": variant_id,
            "variant_value": variant_value,
        }
        existing_bindings = item["variant_bindings"]
        if binding not in existing_bindings:
            existing_bindings.append(binding)


def _linked_rows_for_marketplace_offer(
    marketplace: str,
    marketplace_offer_id: str,
) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import MarketplaceOffer, ProductMapping, SupplierProduct
    except ImportError:
        return []

    with SessionLocal() as db:
        offer_stmt = select(MarketplaceOffer).where(
            MarketplaceOffer.marketplace == marketplace,
            MarketplaceOffer.marketplace_offer_id == marketplace_offer_id,
        )
        offer = db.execute(offer_stmt).scalars().first()
        if offer is None:
            return []

        mapping_stmt = (
            select(ProductMapping, SupplierProduct)
            .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
            .where(ProductMapping.marketplace_offer_id == offer.id)
            .order_by(ProductMapping.id)
        )
        rows = db.execute(mapping_stmt).all()

    linked: list[dict[str, Any]] = []
    for mapping, supplier in rows:
        linked.append(
            {
                "mapping_id": mapping.id,
                "local_supplier_product_id": supplier.id,
                "supplier_product_id": supplier.supplier_product_id,
                "title": supplier.title,
                "denomination": supplier.denomination,
                "enabled": mapping.enabled,
                "marketplace_variant_id": mapping.marketplace_variant_id,
                "marketplace_variant_name": mapping.marketplace_variant_name,
                "marketplace_commission_percent": mapping.marketplace_commission_percent,
                "payment_system_commission_percent": mapping.payment_system_commission_percent,
            }
        )
    return linked


def _print_linked_rows_for_offer(marketplace: str, marketplace_offer_id: str) -> None:
    rows = _linked_rows_for_marketplace_offer(marketplace, marketplace_offer_id)
    if not rows:
        print("Currently linked rows for this marketplace offer: none.")
        return
    print("Currently linked rows for this marketplace offer:")
    for row in rows:
        print(
            f"- mapping {row['mapping_id']}: local {row['local_supplier_product_id']} | "
            f"supplier_id={row['supplier_product_id']} | "
            f"{row['title']} | denomination={row['denomination'] or ''} | "
            f"variant_id={row['marketplace_variant_id']} | "
            f"variant_name={row['marketplace_variant_name'] or ''} | enabled={row['enabled']} | "
            f"marketplace_commission={row['marketplace_commission_percent']} | "
            f"payment_system_commission={row['payment_system_commission_percent']}"
        )


def _normalize_currency_code(value: str | None) -> str | None:
    if not value:
        return None
    raw = re.sub(r"[^A-Z]", "", value.upper())
    if not raw:
        return None
    aliases = {
        "TL": "TRY",
        "RUR": "RUB",
        "USDT": "USD",
    }
    return aliases.get(raw, raw)


def _normalize_amount_text(value: str) -> str:
    text = value.replace(",", ".").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _extract_denomination_signature(*texts: str | None) -> str | None:
    pattern = re.compile(r"(\d+(?:[.,]\d+)?)\s*([A-Za-z]{2,5})")
    for text in texts:
        if not text:
            continue
        for match in pattern.finditer(text):
            amount = _normalize_amount_text(match.group(1))
            currency = _normalize_currency_code(match.group(2))
            if amount and currency:
                return f"{amount} {currency}"
    return None


def _product_denomination_signature(product: Any) -> str | None:
    return _extract_denomination_signature(
        str(getattr(product, "denomination", "") or ""),
        str(getattr(product, "title", "") or ""),
        str(getattr(product, "category", "") or ""),
    )


def _snapshot_denomination_signature(snapshot: dict[str, Any]) -> str | None:
    return _extract_denomination_signature(
        str(snapshot.get("denomination") or ""),
        str(snapshot.get("title") or ""),
    )


_SINGLE_OFFER_GENERIC_TOKENS = {
    "account",
    "accounts",
    "activation",
    "card",
    "cards",
    "code",
    "codes",
    "digital",
    "direct",
    "gift",
    "key",
    "keys",
    "online",
    "playstation",
    "product",
    "ps",
    "psn",
    "recharge",
    "store",
    "steam",
    "test",
    "testing",
    "top",
    "topup",
    "up",
    "wallet",
}


_CURRENCY_REGION_TOKENS = {
    "RUB": {"rub", "rur", "ru", "cis", "russia", "russian"},
    "TRY": {"try", "tl", "tr", "turkey", "turkiye"},
    "PHP": {"php", "philippines", "philippine"},
    "USD": {"usd", "us", "usa", "america", "american"},
    "EUR": {"eur", "euro", "eu", "europe", "european"},
}


def _meaningful_match_tokens(value: object) -> set[str]:
    return {
        token
        for token in _normalize_search_text(value).split()
        if token and token not in _SINGLE_OFFER_GENERIC_TOKENS
    }


def _snapshot_match_text(snapshot: dict[str, Any]) -> str:
    option_names = snapshot.get("option_parameter_names")
    option_text = ""
    if isinstance(option_names, list):
        option_text = " ".join(str(item) for item in option_names)

    return " ".join(
        [
            str(snapshot.get("title") or ""),
            str(snapshot.get("denomination") or ""),
            str(snapshot.get("buyer_input_label") or ""),
            str(snapshot.get("detected_buyer_input_label") or ""),
            option_text,
        ]
    )


def _currency_hints_from_text(value: object) -> set[str]:
    hints: set[str] = set()
    for token in _normalize_search_text(value).split():
        currency = _normalize_currency_code(token.upper())
        if currency in _CURRENCY_REGION_TOKENS:
            hints.add(currency)
    return hints


def _is_direct_service_product(product: Any) -> bool:
    if _product_denomination_signature(product) is not None:
        return False
    category = _normalize_search_text(getattr(product, "category", None) or "")
    return "gift card" not in category


def _direct_service_products(products: list[Any]) -> list[Any]:
    return sorted(
        [product for product in products if _is_direct_service_product(product)],
        key=lambda item: int(item.id),
    )


def _product_matches_currency_hint(product: Any, currency: str) -> bool:
    region_tokens = _CURRENCY_REGION_TOKENS.get(currency, set())
    if not region_tokens:
        return False
    product_tokens = set(_product_search_blob(product).split())
    return bool(product_tokens & region_tokens)


def _products_matching_input_currency_hint(
    products: list[Any],
    snapshot: dict[str, Any],
) -> list[Any]:
    currency_hints = _currency_hints_from_text(_snapshot_match_text(snapshot))
    if not currency_hints:
        return []

    direct_products = _direct_service_products(products)
    if not direct_products:
        return []

    matches = [
        product
        for product in direct_products
        if any(_product_matches_currency_hint(product, currency) for currency in currency_hints)
    ]
    return sorted(matches, key=lambda item: int(item.id))


def _products_matching_snapshot_single_offer(
    products: list[Any],
    snapshot: dict[str, Any],
) -> list[Any]:
    candidate_products = products
    snapshot_signature = _snapshot_denomination_signature(snapshot)
    if snapshot_signature:
        signature_matches = _products_matching_variant_exact(products, snapshot_signature)
        if len(signature_matches) == 1:
            return signature_matches
        if signature_matches:
            candidate_products = signature_matches

    snapshot_text = _normalize_search_text(
        _snapshot_match_text(snapshot)
    )
    snapshot_tokens = _meaningful_match_tokens(snapshot_text)
    matches: list[Any] = []
    for product in candidate_products:
        product_texts = [
            str(getattr(product, "title", "") or ""),
            str(getattr(product, "denomination", "") or ""),
        ]
        normalized_product_texts = [
            _normalize_search_text(text)
            for text in product_texts
            if _normalize_search_text(text)
        ]
        if any(
            product_text
            and len(product_text) >= 4
            and product_text in snapshot_text
            for product_text in normalized_product_texts
        ):
            matches.append(product)
            continue

        product_tokens: set[str] = set()
        for product_text in product_texts:
            product_tokens.update(_meaningful_match_tokens(product_text))
        if product_tokens and product_tokens.issubset(snapshot_tokens):
            matches.append(product)

    return sorted(matches, key=lambda item: int(item.id))


def _auto_select_single_offer_product(
    reference: GEngineProductReference,
    products: list[Any],
    snapshot: dict[str, Any] | None,
) -> tuple[Any | None, str | None]:
    if len(products) == 1:
        return products[0], "only imported row matched the G-Engine reference"

    if snapshot is not None:
        snapshot_matches = _products_matching_snapshot_single_offer(products, snapshot)
        if len(snapshot_matches) == 1:
            return snapshot_matches[0], "marketplace title/denomination matched one local row"

        currency_matches = _products_matching_input_currency_hint(products, snapshot)
        if len(currency_matches) == 1:
            return currency_matches[0], "marketplace buyer-input currency matched one direct row"

    direct_id_matches = [
        product
        for product in products
        if _product_direct_supplier_id(product) == reference.product_id
    ]
    if len(direct_id_matches) == 1:
        return direct_id_matches[0], "G-Engine product ID matched one supplier row"

    specific_products = [
        product
        for product in products
        if _is_specific_reference_product(product, reference)
        and _product_denomination_signature(product) is None
    ]
    if len(specific_products) == 1:
        return specific_products[0], "parent G-Engine product matched one non-denomination row"

    return None, None


def _single_offer_manual_candidates(
    reference: GEngineProductReference,
    products: list[Any],
) -> list[Any]:
    if reference.kind == "recharge":
        direct_products = _direct_service_products(products)
        if direct_products:
            return direct_products
    return products


def _reference_query_hint(reference: GEngineProductReference, products: list[Any]) -> str:
    if reference.slug:
        return _normalize_search_text(reference.slug)
    if products:
        return _normalize_search_text(str(getattr(products[0], "title", "") or ""))
    return ""


def _products_for_reference(reference: GEngineProductReference, products: list[Any]) -> list[Any]:
    id_matches = [
        product
        for product in products
        if _product_matches_reference_id(product, reference)
    ]
    if id_matches:
        return _sort_products_for_reference(id_matches, reference)

    matches = [
        product
        for product in products
        if _product_matches_reference_slug(product, reference)
    ]
    return _sort_products_for_reference(matches, reference)


def _choose_reference_products_interactively() -> tuple[GEngineProductReference, list[Any]] | None:
    try:
        products = _load_recent_products(limit=1000)
    except RuntimeError as exc:
        print(exc)
        return None

    if not products:
        print("No imported products yet. Run product sync first.")
        return None

    while True:
        raw_reference = input(
            "\nPaste G-Engine product link or product ID "
            "(examples: https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey, "
            "https://b2b.g-engine.net/en/recharge/1/steam, "
            "https://b2b.g-engine.net/en/gifts/4451080/vituber-melody): "
        ).strip()
        reference = _parse_gengine_reference(raw_reference)
        if reference is None:
            print("Could not read a G-Engine product ID from that value. Paste the full link.")
            continue

        matches = _products_for_reference(reference, products)
        if matches:
            return reference, matches

        print(
            f"No imported rows matched G-Engine {reference.kind} product ID "
            f"{reference.product_id}."
        )
        if reference.kind in {"shop", "unknown"} and _ask_bool(
            "Try importing this G-Engine product ID now?",
            default=True,
        ):
            imported = _import_gengine_reference(reference)
            print(f"Imported rows: {imported}")
            products = _load_recent_products(limit=2000)
            matches = _products_for_reference(reference, products)
            if matches:
                return reference, matches

        print("If it still does not appear, this G-Engine section may not be")
        print("imported by the current integration yet.")


def _list_marketplace_offer_snapshots(
    marketplace: str,
    *,
    query: str,
) -> list[dict[str, Any]]:
    try:
        from app.clients import DigisellerClient, GGSELClient
        from app.config import get_settings
    except ImportError:
        return []

    settings = get_settings()
    client: Any | None = None
    try:
        if marketplace == "digiseller" and settings.digiseller_api_key:
            client = DigisellerClient(
                settings.digiseller_base_url,
                settings.digiseller_api_key,
                seller_id=settings.digiseller_seller_id,
                task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
                task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
            )
        elif marketplace == "ggsel" and settings.ggsel_api_key:
            client = GGSELClient(
                settings.ggsel_base_url,
                settings.ggsel_api_key,
                seller_id=settings.ggsel_seller_id,
            )
        else:
            return []

        try:
            offers = client.list_offers()
        except NotImplementedError:
            return []
        if not isinstance(offers, list):
            return []

        normalized_query = _normalize_search_text(query)
        snapshots: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in offers:
            if not isinstance(raw, dict):
                continue
            offer_id = _extract_marketplace_offer_id(raw)
            if offer_id is None or offer_id in seen_ids:
                continue
            fields = _extract_marketplace_snapshot_fields(raw)
            fields["marketplace_offer_id"] = offer_id
            fields["raw"] = raw

            if normalized_query:
                blob = _normalize_search_text(
                    " ".join(
                        (
                            str(offer_id),
                            str(fields.get("title") or ""),
                            str(fields.get("denomination") or ""),
                        )
                    )
                )
                if normalized_query not in blob:
                    continue

            snapshots.append(fields)
            seen_ids.add(offer_id)
        return snapshots
    except Exception:
        return []
    finally:
        if client is not None:
            client.close()


def _load_snapshots_by_ids(marketplace: str, offer_ids: list[str]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for offer_id in offer_ids:
        if offer_id in seen_ids:
            continue
        snapshot, _error = _fetch_marketplace_offer_snapshot(marketplace, offer_id)
        if snapshot is None:
            continue
        snapshot["marketplace_offer_id"] = offer_id
        snapshots.append(snapshot)
        seen_ids.add(offer_id)
    return snapshots


def _auto_match_products_and_snapshots(
    products: list[Any],
    snapshots: list[dict[str, Any]],
) -> tuple[list[tuple[Any, dict[str, Any], str]], list[Any], list[dict[str, Any]], list[str]]:
    product_by_sig: dict[str, list[Any]] = {}
    snapshot_by_sig: dict[str, list[dict[str, Any]]] = {}

    for product in products:
        signature = _product_denomination_signature(product)
        if signature is None:
            continue
        product_by_sig.setdefault(signature, []).append(product)

    for snapshot in snapshots:
        signature = _snapshot_denomination_signature(snapshot)
        if signature is None:
            continue
        snapshot_by_sig.setdefault(signature, []).append(snapshot)

    matches: list[tuple[Any, dict[str, Any], str]] = []
    matched_product_ids: set[int] = set()
    matched_snapshot_ids: set[str] = set()
    ambiguous: list[str] = []

    signatures = sorted(set(product_by_sig) & set(snapshot_by_sig))
    for signature in signatures:
        product_group = product_by_sig[signature]
        snapshot_group = snapshot_by_sig[signature]
        if len(product_group) == 1 and len(snapshot_group) == 1:
            product = product_group[0]
            snapshot = snapshot_group[0]
            offer_id = str(snapshot.get("marketplace_offer_id") or "")
            matches.append((product, snapshot, signature))
            matched_product_ids.add(int(product.id))
            if offer_id:
                matched_snapshot_ids.add(offer_id)
        else:
            ambiguous.append(signature)

    unmatched_products = [product for product in products if int(product.id) not in matched_product_ids]
    unmatched_snapshots = [
        snapshot
        for snapshot in snapshots
        if str(snapshot.get("marketplace_offer_id") or "") not in matched_snapshot_ids
    ]

    return matches, unmatched_products, unmatched_snapshots, ambiguous


def _snapshot_label(snapshot: dict[str, Any]) -> str:
    return (
        f"[offer {snapshot.get('marketplace_offer_id')}] "
        f"{snapshot.get('title') or '(no title)'} | denomination={snapshot.get('denomination') or ''} "
        f"| price={snapshot.get('price') if snapshot.get('price') is not None else ''} "
        f"{snapshot.get('currency') or ''}"
    )


def _snapshot_price_or_suggestion(
    product: Any,
    snapshot: dict[str, Any],
    currency: str,
    *,
    marketplace: str,
) -> Decimal | None:
    raw_price = snapshot.get("price")
    if raw_price is not None:
        try:
            price = Decimal(str(raw_price))
            if price > 0:
                return price
        except (InvalidOperation, ValueError):
            pass
    return _suggest_price(product, currency, marketplace=marketplace)


def action_auto_link() -> int:
    _print_header("Auto-Link Offers")
    print(
        "This fetches marketplace offers and links them to all matched G-Engine sub-items "
        "by denomination signature (for example 250 TRY, 500 TRY)."
    )

    marketplace = _ask_text("Marketplace/listing platform (digiseller or ggsel)", default="digiseller")
    marketplace = marketplace.lower()
    if marketplace not in VALID_MARKETPLACES:
        print(f"Marketplace must be one of: {', '.join(VALID_MARKETPLACES)}.")
        return 1

    chosen = _choose_reference_products_interactively()
    if chosen is None:
        return 1
    reference, products = chosen

    print(f"\nImported rows for G-Engine {reference.kind} product ID {reference.product_id}:")
    for product in products:
        signature = _product_denomination_signature(product)
        print(f"{_product_label(product)} | signature={signature or '(none)'}")

    ids_text = _ask_text(
        "Marketplace offer IDs (comma-separated, blank = auto-discover by keyword)",
        required=False,
    )
    snapshots: list[dict[str, Any]] = []
    if ids_text:
        offer_ids = [
            offer_id.strip()
            for offer_id in re.split(r"[,\s]+", ids_text)
            if offer_id.strip()
        ]
        snapshots = _load_snapshots_by_ids(marketplace, offer_ids)
    else:
        default_query = _reference_query_hint(reference, products)
        query = _ask_text(
            "Marketplace discovery keyword",
            default=default_query,
            required=False,
        )
        snapshots = _list_marketplace_offer_snapshots(marketplace, query=query)

    if not snapshots:
        print("No marketplace offers were found for auto-linking.")
        print("Try providing explicit offer IDs.")
        return 1

    print(f"\nMarketplace offers loaded: {len(snapshots)}")
    for snapshot in snapshots[:50]:
        signature = _snapshot_denomination_signature(snapshot)
        print(f"{_snapshot_label(snapshot)} | signature={signature or '(none)'}")
    if len(snapshots) > 50:
        print(f"...and {len(snapshots) - 50} more offers.")

    matches, unmatched_products, unmatched_snapshots, ambiguous = _auto_match_products_and_snapshots(
        products,
        snapshots,
    )
    if not matches:
        print("\nNo unambiguous denomination matches were found.")
        if ambiguous:
            print("Ambiguous signatures detected:")
            for signature in ambiguous:
                print(f"- {signature}")
        return 1

    print(f"\nProposed links: {len(matches)}")
    for product, snapshot, signature in matches:
        print(
            f"- {_product_label(product)} <-> {_snapshot_label(snapshot)} | signature={signature}"
        )
    if ambiguous:
        print("\nAmbiguous signatures skipped:")
        for signature in ambiguous:
            print(f"- {signature}")
    if unmatched_products:
        print(f"\nUnmatched G-Engine rows: {len(unmatched_products)}")
    if unmatched_snapshots:
        print(f"Unmatched marketplace offers: {len(unmatched_snapshots)}")

    if not _ask_bool("Create/update these links now?", default=True):
        print("Cancelled.")
        return 0

    created_or_updated = 0
    skipped = 0
    for product, snapshot, _signature in matches:
        offer_id = str(snapshot.get("marketplace_offer_id") or "").strip()
        if not offer_id:
            skipped += 1
            print(f"Skipped local {product.id}: missing marketplace offer id.")
            continue

        currency = str(snapshot.get("currency") or "RUB").upper()
        if currency not in VALID_CURRENCIES:
            currency = "RUB"

        price = _snapshot_price_or_suggestion(
            product,
            snapshot,
            currency,
            marketplace=marketplace,
        )
        if price is None:
            skipped += 1
            print(f"Skipped local {product.id}: no valid marketplace/suggested price.")
            continue

        requires_buyer_input = bool(snapshot.get("requires_buyer_input"))
        buyer_input_label = (
            str(snapshot.get("buyer_input_label"))
            if snapshot.get("buyer_input_label")
            else None
        )
        buyer_input_help = (
            str(snapshot.get("buyer_input_help"))
            if snapshot.get("buyer_input_help")
            else None
        )
        if requires_buyer_input and not buyer_input_label:
            buyer_input_label = _default_buyer_input_label(product)
        if requires_buyer_input and not buyer_input_help and buyer_input_label:
            buyer_input_help = _default_buyer_input_help(buyer_input_label)

        title = str(snapshot.get("title") or _default_offer_title(product))
        offer_enabled = bool(snapshot.get("is_enabled")) if snapshot.get("is_enabled") is not None else False

        try:
            _upsert_manual_link(
                marketplace=marketplace,
                marketplace_offer_id=offer_id,
                supplier_product_id=int(product.id),
                title=title,
                price=price,
                currency=currency,
                offer_enabled=offer_enabled,
                requires_buyer_input=requires_buyer_input,
                buyer_input_label=buyer_input_label,
                buyer_input_help=buyer_input_help,
                mapping_enabled=True,
            )
            created_or_updated += 1
        except Exception as exc:  # noqa: BLE001 - continue batch linking.
            skipped += 1
            print(f"Skipped local {product.id} <-> offer {offer_id}: {exc}")

    print("\nAuto-link finished.")
    print(f"links_created_or_updated: {created_or_updated}")
    print(f"links_skipped: {skipped}")
    return 0 if created_or_updated > 0 else 1


def _choose_product_interactively() -> Any | None:
    try:
        products = _load_recent_products(limit=500)
    except RuntimeError as exc:
        print(exc)
        return None

    if not products:
        print("No imported products yet. Run product sync first.")
        return None

    next_reference: GEngineProductReference | None = None
    while True:
        if next_reference is None:
            raw_reference = input(
                "\nPaste G-Engine product link or product ID "
                "(examples: https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey, "
                "https://b2b.g-engine.net/en/gifts/4451080/vituber-melody): "
            ).strip()
            reference = _parse_gengine_reference(raw_reference)
            if reference is None:
                print("Could not read a G-Engine product ID from that value. Paste the full link.")
                continue
        else:
            reference = next_reference
            next_reference = None
            print(f"\nLooking up: {reference.source}")

        matches = [
            product for product in products if _product_matches_reference(product, reference)
        ]
        matches = _sort_products_for_reference(matches, reference)
        if not matches:
            print(
                f"No imported rows matched G-Engine {reference.kind} product ID "
                f"{reference.product_id}."
            )
            if reference.kind in {"shop", "unknown"}:
                if _ask_bool("Try importing this G-Engine product ID now?", default=True):
                    imported = _import_gengine_reference(reference)
                    print(f"Imported rows: {imported}")
                    products = _load_recent_products(limit=1000)
                    next_reference = reference
                    continue

            print("If it still does not appear, this G-Engine section may not be")
            print("imported by the current integration yet.")
            continue

        visible_matches = matches[:50]
        print(
            f"\nImported rows for G-Engine {reference.kind} product ID "
            f"{reference.product_id}:"
        )
        for product in visible_matches:
            print(_product_label(product))

        if len(matches) > len(visible_matches):
            print(f"...and {len(matches) - len(visible_matches)} more rows.")

        selected = input(
            "\nEnter local row id from [local ...] or paste another G-Engine link/id: "
        ).strip()
        if selected.lower() in {"s", "search", "again"}:
            continue
        if not selected.isdigit():
            next_reference = _parse_gengine_reference(selected)
            if next_reference is None:
                print("Please enter a local numeric product id or another G-Engine link.")
                continue
            continue

        product_id = int(selected)
        selected_product = next((product for product in matches if product.id == product_id), None)
        if selected_product is None:
            print("That product id is not in the current search results.")
            continue
        return selected_product


def _upsert_manual_link(
    *,
    marketplace: str,
    marketplace_offer_id: str,
    supplier_product_id: int,
    title: str,
    price: Decimal,
    currency: str,
    offer_enabled: bool,
    requires_buyer_input: bool,
    buyer_input_label: str | None,
    buyer_input_help: str | None,
    mapping_enabled: bool,
    marketplace_variant_id: int | None = None,
    marketplace_variant_name: str | None = None,
    marketplace_commission_percent: Decimal | None = None,
    payment_system_commission_percent: Decimal | None = None,
    min_margin_percent: Decimal | None = None,
    min_profit_usd: Decimal | None = None,
    max_price: Decimal | None = None,
) -> dict[str, Any]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import MarketplaceOffer, ProductMapping, SupplierProduct

    with SessionLocal() as db:
        supplier = db.get(SupplierProduct, supplier_product_id)
        if supplier is None:
            raise ValueError("Supplier product not found.")

        offer_stmt = select(MarketplaceOffer).where(
            MarketplaceOffer.marketplace == marketplace,
            MarketplaceOffer.marketplace_offer_id == marketplace_offer_id,
        )
        offer = db.execute(offer_stmt).scalars().first()
        offer_created = False

        if offer is None:
            offer = MarketplaceOffer(
                marketplace=marketplace,
                marketplace_offer_id=marketplace_offer_id,
                supplier_product_id=supplier_product_id,
                title=title,
                price=price,
                currency=currency,
                is_enabled=offer_enabled,
                requires_buyer_input=requires_buyer_input,
                buyer_input_label=buyer_input_label,
                buyer_input_help=buyer_input_help,
                last_synced_at=datetime.now(timezone.utc),
            )
            db.add(offer)
            db.flush()
            offer_created = True
        else:
            offer.supplier_product_id = supplier_product_id
            offer.title = title
            offer.price = price
            offer.currency = currency
            offer.is_enabled = offer_enabled
            offer.requires_buyer_input = requires_buyer_input
            offer.buyer_input_label = buyer_input_label
            offer.buyer_input_help = buyer_input_help
            offer.last_synced_at = datetime.now(timezone.utc)
            db.add(offer)
            db.flush()

        mapping_stmt = select(ProductMapping).where(
            ProductMapping.supplier_product_id == supplier_product_id,
            ProductMapping.marketplace_offer_id == offer.id,
        )
        mapping = db.execute(mapping_stmt).scalars().first()
        mapping_created = False

        if mapping is None:
            mapping = ProductMapping(
                supplier_product_id=supplier_product_id,
                marketplace_offer_id=offer.id,
                enabled=mapping_enabled,
                marketplace_variant_id=marketplace_variant_id,
                marketplace_variant_name=marketplace_variant_name,
                marketplace_commission_percent=marketplace_commission_percent,
                payment_system_commission_percent=payment_system_commission_percent,
                min_margin_percent=min_margin_percent,
                min_profit_usd=min_profit_usd,
                max_price=max_price,
            )
            db.add(mapping)
            mapping_created = True
        else:
            mapping.enabled = mapping_enabled
            mapping.marketplace_variant_id = marketplace_variant_id
            mapping.marketplace_variant_name = marketplace_variant_name
            mapping.marketplace_commission_percent = marketplace_commission_percent
            mapping.payment_system_commission_percent = payment_system_commission_percent
            mapping.min_margin_percent = min_margin_percent
            mapping.min_profit_usd = min_profit_usd
            mapping.max_price = max_price
            db.add(mapping)

        db.commit()
        db.refresh(offer)
        db.refresh(mapping)

        return {
            "offer_created": offer_created,
            "mapping_created": mapping_created,
            "offer_id": offer.id,
            "mapping_id": mapping.id,
        }


def action_manual_link() -> int:
    _print_header("Manual Link Offer")
    print(
        "This links an existing marketplace offer to imported G-Engine rows. "
        "It does not create or edit marketplace listings."
    )
    print(
        "When the marketplace offer has option values (for example 250 TRY, 500 TRY), "
        "the command can auto-map the whole page by exact denomination/signature."
    )

    marketplace = _ask_text("Marketplace/listing platform (digiseller or ggsel)", default="digiseller")
    marketplace = marketplace.lower()
    if marketplace not in VALID_MARKETPLACES:
        print(f"Marketplace must be one of: {', '.join(VALID_MARKETPLACES)}.")
        return 1
    try:
        from app.config import get_settings
    except ImportError as exc:
        print(f"Could not import app config: {exc}")
        return 1
    settings = get_settings()

    chosen = _choose_reference_products_interactively()
    if chosen is None:
        return 1
    reference, reference_products = chosen

    print(
        f"\nLoaded {len(reference_products)} imported row(s) for "
        f"G-Engine {reference.kind} product ID {reference.product_id}."
    )

    marketplace_offer_id = _ask_text("Marketplace offer/product id")

    snapshot, snapshot_error = _fetch_marketplace_offer_snapshot(marketplace, marketplace_offer_id)
    option_variant_entries: list[dict[str, str]] = []
    if snapshot is None:
        print("Could not fetch marketplace item details by ID; continuing with manual values.")
        if snapshot_error:
            print(f"Lookup reason: {snapshot_error}")
    else:
        print("Fetched marketplace item by ID:")
        print(f"- title: {snapshot.get('title') or '(unknown)'}")
        print(f"- denomination: {snapshot.get('denomination') or '(not provided)'}")
        print(f"- price: {snapshot.get('price') if snapshot.get('price') is not None else '(unknown)'}")
        print(f"- currency: {snapshot.get('currency') or '(unknown)'}")
        option_parameter_names = snapshot.get("option_parameter_names")
        if isinstance(option_parameter_names, list) and option_parameter_names:
            unique_parameter_names = list(dict.fromkeys(str(item) for item in option_parameter_names))
            print(f"- parameter names: {', '.join(unique_parameter_names[:12])}")
        raw_variant_entries = snapshot.get("option_variant_entries")
        if isinstance(raw_variant_entries, list):
            option_variant_entries = [
                entry
                for entry in raw_variant_entries
                if isinstance(entry, dict)
                and entry.get("variant_value")
            ]
        if option_variant_entries:
            print("- selectable option values:")
            for entry in option_variant_entries[:20]:
                option_label = str(entry.get("option_label") or "Option")
                variant_value = str(entry.get("variant_value") or "")
                print(f"  {option_label}: {variant_value}")
            if len(option_variant_entries) > 20:
                print(f"  ...and {len(option_variant_entries) - 20} more values.")
        else:
            option_variants = snapshot.get("option_variants")
            if isinstance(option_variants, list) and option_variants:
                unique_variants = list(dict.fromkeys(str(item) for item in option_variants))
                print(f"- option variants: {', '.join(unique_variants[:12])}")

    _print_linked_rows_for_offer(marketplace, marketplace_offer_id)

    targets_by_product_id: dict[int, dict[str, Any]] = {}
    if option_variant_entries:
        auto_matches, unmatched_entries, ambiguous_entries = _auto_match_option_entries_to_products(
            reference_products,
            option_variant_entries,
        )
        for item in auto_matches:
            entry = item["entry"]
            raw_variant_id = entry.get("variant_id")
            variant_id = int(raw_variant_id) if isinstance(raw_variant_id, int) else None
            _add_link_target(
                targets_by_product_id,
                product=item["product"],
                option_description=str(item["option_description"]),
                variant_id=variant_id,
                variant_value=str(entry.get("variant_value") or ""),
            )

        print(
            "\nAuto-map summary: "
            f"matched {len(auto_matches)}/{len(option_variant_entries)} option values."
        )
        if unmatched_entries:
            print("Unmatched option values (skipped):")
            for item in unmatched_entries[:20]:
                entry = item["entry"]
                option_label = str(entry.get("option_label") or "Option")
                variant_value = str(entry.get("variant_value") or "")
                print(f"- {option_label}: {variant_value}")
            if len(unmatched_entries) > 20:
                print(f"...and {len(unmatched_entries) - 20} more unmatched values.")
        if ambiguous_entries:
            print("Ambiguous option values (skipped, multiple local matches):")
            for item in ambiguous_entries[:20]:
                entry = item["entry"]
                option_label = str(entry.get("option_label") or "Option")
                variant_value = str(entry.get("variant_value") or "")
                product_ids = ", ".join(str(product.id) for product in item["products"][:5])
                print(f"- {option_label}: {variant_value} -> local ids {product_ids}")
            if len(ambiguous_entries) > 20:
                print(f"...and {len(ambiguous_entries) - 20} more ambiguous values.")

    if not targets_by_product_id:
        if option_variant_entries:
            print(
                "No option values could be auto-mapped. "
                "Make denomination text match exactly between marketplace and G-Engine "
                "(for example 250 TRY) and retry."
            )
            return 1

        selected_product, selection_reason = _auto_select_single_offer_product(
            reference,
            reference_products,
            snapshot,
        )
        if selected_product is not None:
            print("\nNo option-value list found on marketplace item.")
            print(
                "Auto-selected single-offer local row "
                f"{selected_product.id}: {selection_reason}."
            )
            print(_product_label(selected_product))
        else:
            manual_candidates = _single_offer_manual_candidates(reference, reference_products)
            print("\nNo option-value list found on marketplace item. Select one local row to link:")
            if len(manual_candidates) < len(reference_products):
                print(
                    "Showing direct recharge/top-up candidates only; "
                    "gift-card denomination rows were hidden from this prompt."
                )
            _print_reference_products(manual_candidates, limit=25)
            selected_product = _choose_product_from_reference_rows(
                manual_candidates,
                prompt="Local row id",
                default_product_id=None,
                allow_skip=False,
            )
            if selected_product is None:
                print("Cancelled.")
                return 1
        _add_link_target(
            targets_by_product_id,
            product=selected_product,
            option_description=None,
        )

    targets = list(targets_by_product_id.values())
    primary_product = targets[0]["product"]

    if snapshot and len(targets) == 1 and not _snapshot_matches_selected_product(primary_product, snapshot):
        print("Warning: selected G-Engine denomination does not match fetched marketplace item.")
        print(f"- G-Engine denomination: {getattr(primary_product, 'denomination', None) or '(none)'}")
        print(f"- marketplace denomination: {snapshot.get('denomination') or '(not provided)'}")
        print(f"- marketplace title: {snapshot.get('title') or '(not provided)'}")
        if not _ask_bool("Continue linking anyway?", default=False):
            print("Cancelled.")
            return 0

    currency_default = snapshot.get("currency") if snapshot else None
    if currency_default not in VALID_CURRENCIES:
        currency_default = "RUB"

    currency = _ask_text("Marketplace currency", default=currency_default).upper()
    if currency not in VALID_CURRENCIES:
        print(f"Currency must be one of: {', '.join(VALID_CURRENCIES)}.")
        return 1

    mapping_markup_percent = _ask_optional_percent_as_ratio(
        "Markup rate percent for mapped rows (blank = automatic tier)",
        default_percent=None,
    )
    if mapping_markup_percent is not None:
        print(f"Using custom markup rate: {(mapping_markup_percent * Decimal('100'))}%")
    else:
        print("Using automatic markup tier based on cost.")

    default_marketplace_commission_percent = Decimal("0")
    default_payment_system_commission_percent = Decimal("0")
    if marketplace == "ggsel":
        default_marketplace_commission_percent = (
            Decimal(str(settings.ggsel_default_marketplace_commission_percent)) * Decimal("100")
        )
        default_payment_system_commission_percent = (
            Decimal(str(settings.ggsel_default_payment_system_commission_percent)) * Decimal("100")
        )

    marketplace_commission_percent = _ask_optional_percent_as_ratio(
        "Marketplace commission percentage",
        default_percent=default_marketplace_commission_percent,
    )
    payment_system_commission_percent: Decimal | None = None
    if marketplace == "ggsel":
        payment_system_commission_percent = _ask_optional_percent_as_ratio(
            "Payment-system commission percentage",
            default_percent=default_payment_system_commission_percent,
        )

    suggested_price = _suggest_price(
        primary_product,
        currency,
        marketplace=marketplace,
        markup_percent=mapping_markup_percent,
        marketplace_commission_percent=marketplace_commission_percent,
        payment_system_commission_percent=payment_system_commission_percent,
    )
    if suggested_price is None:
        print("No automatic markup-based price available; enter the current marketplace price.")
        if currency == "RUB":
            print("Tip: set MANUAL_USD_RUB_RATE or enable AUTO_USD_RUB_RATE for RUB suggestions.")
    else:
        print(f"Calculated price from markup settings: {suggested_price} {currency}")

    price_default = snapshot.get("price") if snapshot else None
    if price_default is None:
        price_default = suggested_price
    price = _ask_decimal("Marketplace price", default=price_default)

    title_default = _default_offer_title(primary_product)
    if snapshot and snapshot.get("title"):
        title_default = str(snapshot["title"])
    title = _ask_text("Local offer title", default=title_default)

    offer_enabled_default = bool(snapshot.get("is_enabled")) if snapshot else False
    offer_enabled = _ask_bool("Mark local offer enabled?", default=offer_enabled_default)
    mapping_enabled = _ask_bool("Enable this product mapping?", default=True)

    detected_requires_buyer_input = bool(snapshot.get("detected_requires_buyer_input")) if snapshot else False
    detected_buyer_input_label = (
        str(snapshot.get("detected_buyer_input_label"))
        if snapshot and snapshot.get("detected_buyer_input_label")
        else None
    )
    requires_buyer_input_default = bool(snapshot.get("requires_buyer_input")) if snapshot else False
    if detected_requires_buyer_input:
        requires_buyer_input_default = True

    requires_buyer_input = requires_buyer_input_default
    buyer_input_label: str | None = None
    buyer_input_help: str | None = None
    if requires_buyer_input:
        buyer_input_label = (
            str(snapshot.get("buyer_input_label"))
            if snapshot and snapshot.get("buyer_input_label")
            else None
        )
        if not buyer_input_label:
            buyer_input_label = detected_buyer_input_label or _default_buyer_input_label(primary_product)
        buyer_input_help = (
            str(snapshot.get("buyer_input_help"))
            if snapshot and snapshot.get("buyer_input_help")
            else _default_buyer_input_help(buyer_input_label)
        )
        print(
            f"Buyer input is auto-detected as required. label='{buyer_input_label}'"
        )
    else:
        print("Buyer input is auto-detected as not required.")

    if _ask_bool("Override buyer input settings manually?", default=False):
        requires_buyer_input = _ask_bool(
            "Does this listing require buyer text/input, like a Steam friend code?",
            default=requires_buyer_input,
        )
        if requires_buyer_input:
            buyer_input_label = _ask_text(
                "Buyer input label",
                default=buyer_input_label or _default_buyer_input_label(primary_product),
            )
            buyer_input_help = _ask_text(
                "Buyer input help text",
                default=buyer_input_help or _default_buyer_input_help(buyer_input_label),
                required=False,
            )
        else:
            buyer_input_label = None
            buyer_input_help = None

    print("\nReview:")
    print(f"- marketplace: {marketplace}")
    print(f"- marketplace_offer_id: {marketplace_offer_id}")
    print(f"- local rows selected: {len(targets)}")
    for target in targets:
        product = target["product"]
        option_descriptions = target["option_descriptions"]
        variant_bindings = target["variant_bindings"]
        variant_text = ""
        if variant_bindings:
            binding = variant_bindings[0]
            variant_id = binding.get("variant_id")
            variant_value = binding.get("variant_value")
            variant_text = f" | variant_id={variant_id} | variant_value={variant_value}"
        if option_descriptions:
            joined = "; ".join(option_descriptions)
            print(f"  {_product_label(product)} | option values: {joined}{variant_text}")
        else:
            print(f"  {_product_label(product)}{variant_text}")
    print(f"- title: {title}")
    print(f"- price: {price} {currency}")
    if mapping_markup_percent is not None:
        print(f"- mapping markup rate: {(mapping_markup_percent * Decimal('100'))}%")
    else:
        print("- mapping markup rate: automatic tier")
    if marketplace_commission_percent is not None:
        print(f"- marketplace commission: {(marketplace_commission_percent * Decimal('100'))}%")
    if payment_system_commission_percent is not None:
        print(
            f"- payment-system commission: "
            f"{(payment_system_commission_percent * Decimal('100'))}%"
        )
    print(f"- local offer enabled: {offer_enabled}")
    print(f"- mapping enabled: {mapping_enabled}")
    print(f"- requires buyer input: {requires_buyer_input}")
    if requires_buyer_input:
        print(f"- buyer input label: {buyer_input_label}")
        print(f"- buyer input help: {buyer_input_help}")

    if not _ask_bool("Create/update this local link?", default=True):
        print("Cancelled.")
        return 0

    successful = 0
    failed = 0
    for target in targets:
        product = target["product"]
        variant_bindings = target["variant_bindings"]
        primary_binding = variant_bindings[0] if variant_bindings else {}
        variant_id = primary_binding.get("variant_id")
        marketplace_variant_id = int(variant_id) if isinstance(variant_id, int) else None
        marketplace_variant_name = (
            str(primary_binding.get("variant_value"))
            if primary_binding.get("variant_value") is not None
            else None
        )
        try:
            _upsert_manual_link(
                marketplace=marketplace,
                marketplace_offer_id=marketplace_offer_id,
                supplier_product_id=int(product.id),
                title=title,
                price=price,
                currency=currency,
                offer_enabled=offer_enabled,
                requires_buyer_input=requires_buyer_input,
                buyer_input_label=buyer_input_label,
                buyer_input_help=buyer_input_help,
                mapping_enabled=mapping_enabled,
                marketplace_variant_id=marketplace_variant_id,
                marketplace_variant_name=marketplace_variant_name,
                marketplace_commission_percent=marketplace_commission_percent,
                payment_system_commission_percent=payment_system_commission_percent,
                min_margin_percent=mapping_markup_percent,
                min_profit_usd=None,
                max_price=None,
            )
            successful += 1
        except Exception as exc:  # noqa: BLE001 - continue remaining links.
            failed += 1
            print(f"Link failed for local {product.id}: {exc}")

    print("\nLinked successfully.")
    print(f"links_created_or_updated: {successful}")
    print(f"links_failed: {failed}")
    _print_linked_rows_for_offer(marketplace, marketplace_offer_id)
    if successful > 0 and sys.stdin.isatty() and _ask_bool(
        "Run live price sync now to push calculated prices?",
        default=True,
    ):
        try:
            from app.services.price_sync import run_price_sync

            sync_result = run_price_sync(dry_run=False, refresh_supplier_prices=False)
            print("Price sync result:")
            print(
                f"- prices_updated: {sync_result.get('prices_updated')} | "
                f"skipped: {sync_result.get('skipped')} | errors: {sync_result.get('errors')}"
            )
            print("Note: price sync does not change option visibility or default selections.")
            details = sync_result.get("details")
            if isinstance(details, list):
                skip_details = [
                    item for item in details if isinstance(item, dict) and item.get("action") == "skipped"
                ]
                if skip_details:
                    print("Skipped reasons (first 10):")
                    for item in skip_details[:10]:
                        print(
                            f"- {item.get('marketplace')}:{item.get('marketplace_offer_id')} | "
                            f"mapping={item.get('mapping_id')} | reason={item.get('reason')}"
                        )
                error_details = [
                    item for item in details if isinstance(item, dict) and item.get("action") == "error"
                ]
                if error_details:
                    print("Error reasons (first 10):")
                    for item in error_details[:10]:
                        print(
                            f"- {item.get('marketplace')}:{item.get('marketplace_offer_id')} | "
                            f"mapping={item.get('mapping_id')} | reason={item.get('reason')}"
                        )
        except Exception as exc:  # noqa: BLE001 - keep manual link successful even if sync fails.
            print(f"Price sync failed after linking: {exc}")
    if successful > 0 and sys.stdin.isatty():
        should_sync_stock = _ask_bool(
            "Run live stock sync now to enforce balance-based option visibility?",
            default=True,
        )
        if should_sync_stock:
            try:
                from app.services.stock_sync import run_stock_sync

                stock_result = run_stock_sync()
                print("Stock sync result:")
                _print_stock_sync_result(stock_result)
            except Exception as exc:  # noqa: BLE001 - keep manual link successful even if sync fails.
                print(f"Stock sync failed after linking: {exc}")
        else:
            print(
                "Stock sync skipped. Digiseller option visibility/default state was not changed; "
                "crossed-eye variants remain hidden until stock sync runs successfully."
            )
    return 0 if successful > 0 and failed == 0 else (0 if successful > 0 else 1)


def action_docker_build() -> int:
    if shutil.which("docker") is None:
        print("Docker is not installed or not on PATH.")
        return 1
    return _run_command(("docker", "compose", "build"))


COMMANDS: dict[str, Callable[[], int]] = {
    "check-config": action_check_config,
    "init-db": action_init_db,
    "test": action_test,
    "lint": action_lint,
    "typecheck": action_typecheck,
    "check": action_check,
    "run-api": action_run_api,
    "sync-products": action_sync_products,
    "auto-link": action_auto_link,
    "price-preview": action_price_preview,
    "sync-prices": action_sync_prices,
    "sync-stock": action_sync_stock,
    "sync-orders": action_sync_orders,
    "list-products": action_list_products,
    "list-offers": action_list_offers,
    "manual-link": action_manual_link,
    "manual-link-example": action_manual_link_example,
    "docker-build": action_docker_build,
}


def print_menu() -> None:
    _print_header("Gift-Card VPS Menu")
    for item in MENU_ACTIONS:
        print(f"{item.key}. {item.label} - {item.description}")
    print("0. Exit")


def run_menu() -> int:
    while True:
        print_menu()
        choice = input("\nChoose an option: ").strip()
        if choice in {"0", "q", "quit", "exit"}:
            print("Bye.")
            return 0

        action = next((item for item in MENU_ACTIONS if item.key == choice), None)
        if action is None:
            print("Unknown option. Try again.")
            continue

        code = COMMANDS[action.command]()
        if code != 0:
            print(f"\nAction finished with exit code {code}.")

        input("\nPress Enter to continue...")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Friendly management menu for the VPS app.")
    parser.add_argument(
        "command",
        nargs="?",
        default="menu",
        choices=("menu", *COMMANDS.keys()),
        help="Command to run. Omit for interactive menu.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "menu":
        return run_menu()
    return COMMANDS[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
