from __future__ import annotations

from types import SimpleNamespace

import manage


def test_menu_actions_point_to_existing_commands() -> None:
    missing = [item.command for item in manage.MENU_ACTIONS if item.command not in manage.COMMANDS]
    assert missing == []


def test_parse_args_defaults_to_menu() -> None:
    args = manage.parse_args([])
    assert args.command == "menu"


def test_parse_args_accepts_direct_command() -> None:
    args = manage.parse_args(["check"])
    assert args.command == "check"


def test_parse_args_accepts_manual_link_command() -> None:
    args = manage.parse_args(["manual-link"])
    assert args.command == "manual-link"


def test_product_search_matches_denominations_and_categories() -> None:
    product = SimpleNamespace(
        id=10,
        title="Turkey Gift Card",
        category="Gift Card",
        denomination="500 TL",
        region="TR",
        supplier_product_id="abc-500",
        supplier_stock="5",
    )

    assert manage._product_matches_query(product, "500 tl")
    assert manage._product_matches_query(product, "gift abc")
    assert not manage._product_matches_query(product, "250 tl")


def test_default_offer_title_includes_distinct_denomination() -> None:
    product = SimpleNamespace(title="Turkey Gift Card", denomination="500 TL")
    assert manage._default_offer_title(product) == "Turkey Gift Card - 500 TL"


def test_default_buyer_input_label_prefers_steam_friend_code() -> None:
    product = SimpleNamespace(
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
    )
    assert manage._default_buyer_input_label(product) == "Steam friend code"
