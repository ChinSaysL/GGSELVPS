from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


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
    MenuAction("11", "Manual link help", "manual-link-example", "Print an example link request."),
    MenuAction("12", "Docker build", "docker-build", "Run docker compose build."),
)


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


def action_list_products() -> int:
    _print_header("Imported Products")
    try:
        from sqlalchemy import desc, select

        from app.db import SessionLocal
        from app.models import SupplierProduct
    except ImportError as exc:
        print(f"Could not import app dependencies: {exc}")
        print("Run: python -m pip install -r requirements.txt")
        return 1

    with SessionLocal() as db:
        products = (
            db.execute(select(SupplierProduct).order_by(desc(SupplierProduct.updated_at)).limit(25))
            .scalars()
            .all()
        )

    if not products:
        print("No imported products yet. Run product sync first.")
        return 0

    for product in products:
        print(
            f"[{product.id}] {product.title} | supplier_id={product.supplier_product_id} | "
            f"cost_usd={_format_money(product.cost_usd)} | stock={product.supplier_stock}"
        )
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
            f"price={_format_money(offer.price)} {offer.currency} | enabled={offer.is_enabled}"
        )
    return 0


def action_manual_link_example() -> int:
    _print_header("Manual Link Example")
    print(
        """Create the marketplace offer manually first, then link it:

curl -X POST http://127.0.0.1:8000/offers/manual-link \\
  -H "Content-Type: application/json" \\
  -d "{\\"marketplace\\":\\"digiseller\\",\\"marketplace_offer_id\\":\\"123456\\",\\"supplier_product_id\\":1,\\"title\\":\\"Steam Gift Card 10 USD\\",\\"price\\":\\"999.00\\",\\"currency\\":\\"RUB\\",\\"offer_enabled\\":false,\\"mapping_enabled\\":true}"
"""
    )
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
