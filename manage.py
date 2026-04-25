from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


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
    MenuAction("12", "Manual link help", "manual-link-example", "Print an example link request."),
    MenuAction("13", "Docker build", "docker-build", "Run docker compose build."),
)

VALID_MARKETPLACES = ("digiseller", "ggsel")
VALID_CURRENCIES = ("RUB", "USD")
OPTIONAL_SEARCH_TERMS = {"card", "code", "digital", "gift", "store", "wallet"}
SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "psn": ("playstation", "ps", "sony"),
    "playstation": ("psn", "ps", "sony"),
    "ps": ("playstation", "psn", "sony"),
    "sony": ("playstation", "psn"),
    "tl": ("try", "turkey", "turkish"),
    "try": ("tl", "turkey", "turkish"),
    "turkey": ("try", "tl", "turkish"),
    "turkish": ("turkey", "try", "tl"),
}


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
    print(f"default_currency: {settings.default_currency}")
    print(f"manual_usd_rub_rate configured: {_safe_yes_no(settings.manual_usd_rub_rate is not None)}")
    print(f"gengine_api_key: {mask_secret(settings.gengine_api_key)}")
    print(f"ggsel_api_key: {mask_secret(settings.ggsel_api_key)}")
    print(f"digiseller_api_key: {mask_secret(settings.digiseller_api_key)}")

    missing: list[str] = []
    warnings: list[str] = []
    if not settings.gengine_api_key:
        missing.append("GENGINE_API_KEY")
    if settings.default_currency == "RUB" and settings.manual_usd_rub_rate is None:
        warnings.append("MANUAL_USD_RUB_RATE is needed before RUB price updates are implemented.")

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


def _format_money(value: object) -> str:
    return "" if value is None else str(value)


def _product_label(product: Any) -> str:
    return (
        f"[{product.id}] {product.title} | denomination={product.denomination or ''} | "
        f"category={product.category or ''} | supplier_id={product.supplier_product_id} | "
        f"cost_usd={_format_money(product.cost_usd)} | stock={product.supplier_stock}"
    )


def _product_search_blob(product: Any) -> str:
    parts = (
        product.id,
        product.title,
        product.category,
        product.denomination,
        product.region,
        product.supplier_product_id,
        product.supplier_stock,
        getattr(product, "raw_payload_json", None),
    )
    return _normalize_search_text(" ".join("" if part is None else str(part) for part in parts))


def _normalize_search_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _search_groups(query: str) -> list[tuple[str, set[str]]]:
    terms = [term for term in _normalize_search_text(query).split() if term]
    groups: list[tuple[str, set[str]]] = []
    for term in terms:
        variants = {term, *SEARCH_ALIASES.get(term, ())}
        groups.append((term, variants))
    return groups


def _product_matches_query(product: Any, query: str) -> bool:
    return _product_match_score(product, query) > 0


def _product_match_score(product: Any, query: str) -> int:
    groups = _search_groups(query)
    if not groups:
        return 1

    blob = _product_search_blob(product)
    tokens = set(blob.split())
    required_groups = [
        (term, variants) for term, variants in groups if term not in OPTIONAL_SEARCH_TERMS
    ]
    if not required_groups:
        required_groups = groups

    required_matches = sum(
        1 for _, variants in required_groups if _variant_group_matches(blob, tokens, variants)
    )
    if required_matches != len(required_groups):
        return 0

    optional_matches = sum(
        1
        for term, variants in groups
        if term in OPTIONAL_SEARCH_TERMS and _variant_group_matches(blob, tokens, variants)
    )

    # Prefer products where the important terms match, then use optional/product-detail
    # matches to sort friendlier results first.
    return (required_matches * 100) + (optional_matches * 10) + _product_detail_score(product, groups)


def _variant_group_matches(blob: str, tokens: set[str], variants: set[str]) -> bool:
    return any(_blob_contains(blob, tokens, variant) for variant in variants)


def _product_detail_score(product: Any, groups: list[tuple[str, set[str]]]) -> int:
    detail_fields = (
        ("title", 8),
        ("denomination", 6),
        ("category", 4),
        ("supplier_product_id", 3),
        ("raw_payload_json", 2),
    )
    score = 0
    for field_name, weight in detail_fields:
        value = getattr(product, field_name, None)
        if value is None:
            continue
        field_blob = _normalize_search_text(value)
        field_tokens = set(field_blob.split())
        for _, variants in groups:
            if _variant_group_matches(field_blob, field_tokens, variants):
                score += weight
    return score


def _sort_products_for_query(products: list[Any], query: str) -> list[Any]:
    if not _search_groups(query):
        return products
    return sorted(products, key=lambda product: (-_product_match_score(product, query), product.id))


def _blob_contains(blob: str, tokens: set[str], term: str) -> bool:
    if not term:
        return False
    if term in tokens:
        return True
    if len(term) >= 4:
        return any(term in token or token in term for token in tokens)
    return f" {term} " in f" {blob} "


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
        from app.config import get_settings
        from app.pricing import calculate_final_price_usd, convert_usd_to_currency
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
    if currency == "RUB" and settings.manual_usd_rub_rate is not None:
        return convert_usd_to_currency(
            calculation.final_price_usd,
            "RUB",
            manual_usd_rub_rate=Decimal(str(settings.manual_usd_rub_rate)),
        )
    return None


def _choose_product_interactively() -> Any | None:
    try:
        products = _load_recent_products(limit=500)
    except RuntimeError as exc:
        print(exc)
        return None

    if not products:
        print("No imported products yet. Run product sync first.")
        return None

    next_query: str | None = None
    while True:
        if next_query is None:
            query = input(
                "\nSearch product/sub-item by title, denomination, category, slug, or supplier id "
                "(examples: playstation turkey, 250 TRY, 500 TL, psn; blank shows latest 25): "
            ).strip()
        else:
            query = next_query
            next_query = None
            print(f"\nSearching again for: {query}")

        matches = [
            product for product in products if _product_matches_query(product, query)
        ]
        matches = _sort_products_for_query(matches, query)
        if not matches:
            print("No matches. Try a broader search like 'playstation', 'TRY', 'Roblox', or 'Steam'.")
            continue

        visible_matches = matches[:25]
        print("\nMatching products:")
        for product in visible_matches:
            print(_product_label(product))

        if len(matches) > len(visible_matches):
            print(f"...and {len(matches) - len(visible_matches)} more. Refine the search if needed.")

        selected = input("\nEnter local product id from [] or 's' to search again: ").strip().lower()
        if selected in {"s", "search"}:
            continue
        if not selected.isdigit():
            next_query = selected
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
                last_synced_at=datetime.utcnow(),
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
            offer.last_synced_at = datetime.utcnow()
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
    currency = _ask_text("Marketplace currency", default="RUB").upper()
    if currency not in VALID_CURRENCIES:
        print(f"Currency must be one of: {', '.join(VALID_CURRENCIES)}.")
        return 1

    suggested_price = _suggest_price(product, currency)
    if suggested_price is None:
        print("No automatic price suggestion available; enter the current marketplace price.")
    else:
        print(f"Suggested minimum safe price: {suggested_price} {currency}")

    price = _ask_decimal("Marketplace price", default=suggested_price)
    title = _ask_text("Local offer title", default=_default_offer_title(product))
    offer_enabled = _ask_bool("Mark local offer enabled?", default=False)
    mapping_enabled = _ask_bool("Enable this product mapping?", default=True)
    requires_buyer_input = _ask_bool(
        "Does this listing require buyer text/input, like a Steam friend code?",
        default=False,
    )
    buyer_input_label: str | None = None
    buyer_input_help: str | None = None
    if requires_buyer_input:
        buyer_input_label = _ask_text(
            "Buyer input label",
            default=_default_buyer_input_label(product),
        )
        buyer_input_help = _ask_text(
            "Buyer input help text",
            default=_default_buyer_input_help(buyer_input_label),
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
