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


def test_parse_gengine_shop_reference_from_url() -> None:
    reference = manage._parse_gengine_reference(
        "https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey"
    )

    assert reference == manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey",
    )


def test_parse_gengine_gift_reference_from_url() -> None:
    reference = manage._parse_gengine_reference(
        "https://b2b.g-engine.net/en/gifts/4451080/vituber-melody"
    )

    assert reference is not None
    assert reference.kind == "gifts"
    assert reference.product_id == "4451080"
    assert reference.slug == "vituber-melody"


def test_parse_plain_numeric_reference() -> None:
    reference = manage._parse_gengine_reference("5")

    assert reference == manage.GEngineProductReference(
        kind="unknown",
        product_id="5",
        slug=None,
        source="5",
    )


def test_product_reference_matches_base_product_id_from_raw_payload() -> None:
    product = SimpleNamespace(
        id=130,
        title="250 TRY",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        supplier_product_id="560",
        raw_payload_json='{"id":"560","product_id":"5"}',
    )
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="link",
    )

    assert manage._product_matches_reference(product, reference)


def test_product_reference_matches_base_product_id_field() -> None:
    product = SimpleNamespace(
        id=130,
        title="250 TRY",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        supplier_product_id="5001",
        raw_payload_json='{"id":"5001","base_product_id":"5"}',
    )
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="link",
    )

    assert manage._product_matches_reference(product, reference)


def test_product_reference_matches_direct_supplier_product_id() -> None:
    product = SimpleNamespace(
        id=1,
        title="Steam RUB",
        category="Direct replenishment",
        denomination=None,
        supplier_product_id="1",
        raw_payload_json="{}",
    )
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="link",
    )

    assert manage._product_matches_reference(product, reference)


def test_product_reference_does_not_match_local_database_id() -> None:
    product = SimpleNamespace(
        id=5,
        title="60 BRL",
        category="Gift Card, Roblox",
        denomination="60 BRL",
        supplier_product_id="716",
        raw_payload_json='{"id":"716","product_id":"999"}',
    )
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="link",
    )

    assert not manage._product_matches_reference(product, reference)


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


def test_title_from_slug_formats_readable_title() -> None:
    assert (
        manage._title_from_slug("playstation-store-wallet-turkey")
        == "Playstation Store Wallet Turkey"
    )
