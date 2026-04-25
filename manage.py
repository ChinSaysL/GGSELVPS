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
from decimal import Decimal, InvalidOperation
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
    MenuAction("15", "Manual link help", "manual-link-example", "Print an example link request."),
    MenuAction("16", "Docker build", "docker-build", "Run docker compose build."),
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
    print(f"default_currency: {settings.default_currency}")
    print(f"manual_usd_rub_rate configured: {_safe_yes_no(settings.manual_usd_rub_rate is not None)}")
    print(f"gengine_api_key: {mask_secret(settings.gengine_api_key)}")
    print(f"ggsel_api_key: {mask_secret(settings.ggsel_api_key)}")
    print(f"digiseller_api_key: {mask_secret(settings.digiseller_api_key)}")

    missing: list[str] = []
    warnings: list[str] = []
    if not settings.gengine_api_key:
        missing.append("GENGINE_API_KEY")
    if not settings.scheduler_enabled:
        warnings.append(
            "SCHEDULER_ENABLED is false, so automatic hourly price sync is disabled."
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


def _product_matches_reference(product: Any, reference: GEngineProductReference) -> bool:
    if reference.product_id in _product_reference_ids(product):
        return True
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
  -d "{\\"marketplace\\":\\"digiseller\\",\\"marketplace_offer_id\\":\\"123456\\",\\"supplier_product_id\\":1,\\"title\\":\\"Steam Gift Card 10 USD\\",\\"price\\":\\"999.00\\",\\"currency\\":\\"RUB\\",\\"offer_enabled\\":false,\\"requires_buyer_input\\":true,\\"buyer_input_label\\":\\"Steam friend code\\",\\"buyer_input_help\\":\\"Ask the buyer to provide their Steam friend code before fulfillment.\\",\\"mapping_enabled\\":true}"
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


def _suggest_price(product: Any, currency: str) -> Decimal | None:
    try:
        from app.pricing import calculate_final_price_usd, convert_usd_to_currency
        from app.services.fx import resolve_usd_rub_rate
        from app.config import get_settings
    except ImportError:
        return None

    settings = get_settings()
    calculation = calculate_final_price_usd(
        product.cost_usd,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
    )
    if currency == "USD":
        return calculation.final_price_usd
    if currency == "RUB":
        try:
            usd_rub_rate = resolve_usd_rub_rate(settings)
        except Exception:
            return None
        return convert_usd_to_currency(
            calculation.final_price_usd,
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


def _extract_marketplace_snapshot_fields(payload: dict[str, Any]) -> dict[str, Any]:
    title = _payload_first_str(
        payload,
        (
            "title",
            "name",
            "product_name",
            "good_name",
            "goods_name",
            "caption",
        ),
    )
    denomination = _payload_first_str(payload, ("denomination", "nominal", "variant", "option"))
    currency = _payload_first_str(payload, ("currency", "currency_code", "valute", "curr"))
    price = _payload_first_decimal(payload, ("price", "cost", "amount", "sum"))
    is_enabled = _payload_first_bool(payload, ("enabled", "is_enabled", "active", "is_active"))
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
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            option_label = _payload_first_str(option, ("label", "name"))
            variants = option.get("variants")
            if not isinstance(variants, list):
                if option_label:
                    option_variants.append(option_label)
                continue

            default_variant_text: str | None = None
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                variant_text = _payload_first_str(variant, ("text", "label", "name", "value"))
                if variant_text:
                    option_variants.append(variant_text)
                if default_variant_text is None and _payload_first_bool(
                    variant,
                    ("default", "is_default"),
                ):
                    default_variant_text = variant_text

            if denomination is None and default_variant_text:
                denomination = default_variant_text
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
    }


def _extract_marketplace_offer_id(payload: dict[str, Any]) -> str | None:
    return _payload_first_str(
        payload,
        ("id", "product_id", "productId", "goods_id", "good_id"),
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
            client = DigisellerClient(settings.digiseller_base_url, settings.digiseller_api_key)
        elif marketplace == "ggsel" and settings.ggsel_api_key:
            client = GGSELClient(settings.ggsel_base_url, settings.ggsel_api_key)
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


def _reference_query_hint(reference: GEngineProductReference, products: list[Any]) -> str:
    if reference.slug:
        return _normalize_search_text(reference.slug)
    if products:
        return _normalize_search_text(str(getattr(products[0], "title", "") or ""))
    return ""


def _products_for_reference(reference: GEngineProductReference, products: list[Any]) -> list[Any]:
    matches = [product for product in products if _product_matches_reference(product, reference)]
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
            client = DigisellerClient(settings.digiseller_base_url, settings.digiseller_api_key)
        elif marketplace == "ggsel" and settings.ggsel_api_key:
            client = GGSELClient(settings.ggsel_base_url, settings.ggsel_api_key)
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


def _snapshot_price_or_suggestion(product: Any, snapshot: dict[str, Any], currency: str) -> Decimal | None:
    raw_price = snapshot.get("price")
    if raw_price is not None:
        try:
            price = Decimal(str(raw_price))
            if price > 0:
                return price
        except (InvalidOperation, ValueError):
            pass
    return _suggest_price(product, currency)


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

        price = _snapshot_price_or_suggestion(product, snapshot, currency)
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
            )
            db.add(mapping)
            mapping_created = True
        else:
            mapping.enabled = mapping_enabled
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
        "This links an existing marketplace offer to one exact G-Engine product/sub-item. "
        "It does not create or edit marketplace listings."
    )
    print(
        "If one marketplace listing has several choices like 250 TRY and 500 TRY, "
        "create/link each marketplace product or variant ID separately for now."
    )

    marketplace = _ask_text("Marketplace/listing platform (digiseller or ggsel)", default="digiseller")
    marketplace = marketplace.lower()
    if marketplace not in VALID_MARKETPLACES:
        print(f"Marketplace must be one of: {', '.join(VALID_MARKETPLACES)}.")
        return 1

    product = _choose_product_interactively()
    if product is None:
        return 1

    print(f"\nSelected: {_product_label(product)}")
    marketplace_offer_id = _ask_text("Marketplace offer/product id")

    snapshot, snapshot_error = _fetch_marketplace_offer_snapshot(marketplace, marketplace_offer_id)
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
        option_variants = snapshot.get("option_variants")
        if isinstance(option_variants, list) and option_variants:
            unique_variants = list(dict.fromkeys(str(item) for item in option_variants))
            print(f"- option variants: {', '.join(unique_variants[:12])}")

        if not _snapshot_matches_selected_product(product, snapshot):
            print("Warning: selected G-Engine denomination does not match fetched marketplace item.")
            print(f"- G-Engine denomination: {getattr(product, 'denomination', None) or '(none)'}")
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

    suggested_price = _suggest_price(product, currency)
    if suggested_price is None:
        print("No automatic price suggestion available; enter the current marketplace price.")
        if currency == "RUB":
            print("Tip: set MANUAL_USD_RUB_RATE or enable AUTO_USD_RUB_RATE for RUB suggestions.")
    else:
        print(f"Suggested minimum safe price: {suggested_price} {currency}")

    price_default = snapshot.get("price") if snapshot else None
    if price_default is None:
        price_default = suggested_price
    price = _ask_decimal("Marketplace price", default=price_default)

    title_default = _default_offer_title(product)
    if snapshot and snapshot.get("title"):
        title_default = str(snapshot["title"])
    title = _ask_text("Local offer title", default=title_default)

    offer_enabled_default = bool(snapshot.get("is_enabled")) if snapshot else False
    offer_enabled = _ask_bool("Mark local offer enabled?", default=offer_enabled_default)
    mapping_enabled = _ask_bool("Enable this product mapping?", default=True)

    requires_buyer_input_default = bool(snapshot.get("requires_buyer_input")) if snapshot else False
    requires_buyer_input = _ask_bool(
        "Does this listing require buyer text/input, like a Steam friend code?",
        default=requires_buyer_input_default,
    )
    buyer_input_label: str | None = None
    buyer_input_help: str | None = None
    if requires_buyer_input:
        buyer_input_label_default = (
            str(snapshot.get("buyer_input_label"))
            if snapshot and snapshot.get("buyer_input_label")
            else _default_buyer_input_label(product)
        )
        buyer_input_label = _ask_text(
            "Buyer input label",
            default=buyer_input_label_default,
        )
        buyer_input_help_default = (
            str(snapshot.get("buyer_input_help"))
            if snapshot and snapshot.get("buyer_input_help")
            else _default_buyer_input_help(buyer_input_label)
        )
        buyer_input_help = _ask_text(
            "Buyer input help text",
            default=buyer_input_help_default,
            required=False,
        )

    print("\nReview:")
    print(f"- marketplace: {marketplace}")
    print(f"- marketplace_offer_id: {marketplace_offer_id}")
    print(f"- supplier_product_id: {product.id}")
    print(f"- product/sub-item: {_product_label(product)}")
    print(f"- title: {title}")
    print(f"- price: {price} {currency}")
    print(f"- local offer enabled: {offer_enabled}")
    print(f"- mapping enabled: {mapping_enabled}")
    print(f"- requires buyer input: {requires_buyer_input}")
    if requires_buyer_input:
        print(f"- buyer input label: {buyer_input_label}")
        print(f"- buyer input help: {buyer_input_help}")

    if not _ask_bool("Create/update this local link?", default=False):
        print("Cancelled.")
        return 0

    try:
        result = _upsert_manual_link(
            marketplace=marketplace,
            marketplace_offer_id=marketplace_offer_id,
            supplier_product_id=product.id,
            title=title,
            price=price,
            currency=currency,
            offer_enabled=offer_enabled,
            requires_buyer_input=requires_buyer_input,
            buyer_input_label=buyer_input_label,
            buyer_input_help=buyer_input_help,
            mapping_enabled=mapping_enabled,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should show a readable failure.
        print(f"Manual link failed: {exc}")
        return 1

    print("\nLinked successfully.")
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


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
