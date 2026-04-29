from __future__ import annotations

from decimal import Decimal
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


def test_parse_args_accepts_auto_link_command() -> None:
    args = manage.parse_args(["auto-link"])
    assert args.command == "auto-link"


def test_print_order_sync_result_prioritizes_pending_and_hides_unlinked(capsys) -> None:
    manage._print_order_sync_result(
        {
            "status": "ok",
            "orders_detected": 64,
            "orders_processed": 0,
            "supplier_orders_created": 0,
            "orders_delivered": 0,
            "skipped": 64,
            "errors": 0,
            "details": [
                {
                    "action": "skipped",
                    "marketplace_order_id": "288765912",
                    "reason": "marketplace_offer_not_linked",
                },
                {
                    "action": "skipped",
                    "marketplace_order_id": "290877840",
                    "reason": "awaiting_digiseller_unique_code",
                },
            ],
        }
    )

    output = capsys.readouterr().out
    assert "Pending linked orders" in output
    assert "order=290877840" in output
    assert "marketplace_offer_not_linked" not in output
    assert "order=288765912" not in output


def test_print_product_sync_result_shows_new_products(capsys) -> None:
    manage._print_product_sync_result(
        {
            "status": "ok",
            "supplier": "gengine",
            "marketplace": "ggsel",
            "products_received": 2,
            "products_imported": 2,
            "supplier_products_created": 1,
            "supplier_products_updated": 1,
            "manual_link_required": 2,
            "skipped": 0,
            "provider_unavailable": 0,
            "new_products": [
                {
                    "local_id": 546,
                    "supplier_product_id": "gift:1238840:432909:ua",
                    "title": "Battlefield 1 Revolution",
                    "denomination": "ua",
                    "region": "ua",
                    "cost_usd": "1.496",
                    "stock": "unlimited",
                    "source_type": "gift",
                }
            ],
            "details": [],
        }
    )

    output = capsys.readouterr().out
    assert "New products imported" in output
    assert "local 546" in output
    assert "gift:1238840:432909:ua" in output
    assert "Battlefield 1 Revolution" in output
    assert "cost_usd=1.496" in output


def test_print_product_sync_result_says_no_new_products(capsys) -> None:
    manage._print_product_sync_result(
        {
            "status": "ok",
            "supplier_products_created": 0,
            "new_products": [],
        }
    )

    output = capsys.readouterr().out
    assert "New products imported: none" in output


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


def test_gengine_import_skip_details_include_row_reason() -> None:
    message = manage._format_gengine_import_skip_details(
        {
            "details": [
                {
                    "reason": "non_positive_cost_usd",
                    "supplier_product_id": "gift:1238840:432909:RU",
                    "title": "Battlefield 1",
                    "cost_usd": "0",
                }
            ]
        }
    )

    assert "reason=non_positive_cost_usd" in message
    assert "supplier_id=gift:1238840:432909:RU" in message
    assert "cost_usd=0" in message


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


def test_products_for_reference_prefers_id_matches_over_slug_fallback() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
        SimpleNamespace(
            id=136,
            title="100 PHP",
            category="Gift Card, Steam",
            denomination="100 PHP",
            supplier_product_id="547",
            raw_payload_json='{"product_id":"999"}',
        ),
    ]

    matches = manage._products_for_reference(reference, products)

    assert [product.id for product in matches] == [1]


def test_products_for_shop_reference_keeps_slug_denomination_rows_with_parent_row() -> None:
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="6",
        slug="steam-wallet-code-usa",
        source="https://b2b.g-engine.net/en/shop/6/steam-wallet-code-usa",
    )
    products = [
        SimpleNamespace(
            id=10,
            title="Steam Wallet Code USA",
            category="Gift Card, Steam",
            denomination=None,
            supplier_product_id="6",
            raw_payload_json="{}",
        ),
        SimpleNamespace(
            id=84,
            title="5 USD",
            category="Gift Card, Steam",
            denomination="5 USD",
            supplier_product_id="600",
            raw_payload_json='{"source_slug":"steam-wallet-code-usa"}',
        ),
        SimpleNamespace(
            id=120,
            title="5 USD",
            category="Gift Card, Google",
            denomination="5 USD",
            supplier_product_id="566",
            raw_payload_json='{"source_slug":"google-play-usa"}',
        ),
    ]

    matches = manage._products_for_reference(reference, products)

    assert [product.id for product in matches] == [10, 84]


def test_products_for_reference_recharge_requires_exact_id_metadata() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam Worldwide",
            category="Games, Steam",
            denomination="Wordwide",
            supplier_product_id="719",
            raw_payload_json="{}",
        ),
        SimpleNamespace(
            id=2,
            title="Steam Key RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json="{}",
        ),
    ]

    matches = manage._products_for_reference(reference, products)

    assert matches == []


def test_product_matches_reference_id_shop_ignores_recharge_id_collision() -> None:
    product = SimpleNamespace(
        id=142,
        title="Mobile Legends Bang Bang (Russia)",
        category="Recharge",
        denomination="250 + 25 Diamonds",
        supplier_product_id="recharge:5:250",
        raw_payload_json='{"source_type":"recharge","recharge_product_id":"5"}',
    )
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="link",
    )

    assert not manage._product_matches_reference_id(product, reference)


def test_products_for_reference_shop_skips_recharge_rows_with_same_numeric_id() -> None:
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey",
    )
    products = [
        SimpleNamespace(
            id=142,
            title="Mobile Legends Bang Bang (Russia)",
            category="Recharge",
            denomination="250 + 25 Diamonds",
            supplier_product_id="recharge:5:250",
            raw_payload_json='{"source_type":"recharge","recharge_product_id":"5"}',
        ),
        SimpleNamespace(
            id=300,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
            supplier_product_id="145829",
            raw_payload_json='{"id":"145829","base_product_id":"5"}',
        ),
    ]

    matches = manage._products_for_reference(reference, products)

    assert [product.id for product in matches] == [300]


def test_product_reference_matches_recharge_service_id_in_supplier_id() -> None:
    product = SimpleNamespace(
        id=1,
        title="Steam top up",
        category="Recharge",
        denomination="Variable amount",
        supplier_product_id="recharge:1:500",
        raw_payload_json="{}",
    )
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="link",
    )

    assert manage._product_matches_reference_id(product, reference)


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


def test_mapping_enabled_value_prefers_enabled_field() -> None:
    mapping = SimpleNamespace(enabled=False, is_enabled=True)
    assert manage._mapping_enabled_value(mapping) is False


def test_mapping_enabled_value_supports_legacy_is_enabled_field() -> None:
    mapping = SimpleNamespace(is_enabled=False)
    assert manage._mapping_enabled_value(mapping) is False


def test_mapping_enabled_value_defaults_to_true_when_missing() -> None:
    mapping = SimpleNamespace()
    assert manage._mapping_enabled_value(mapping) is True


def test_apply_manual_recharge_config_updates_cost_quantity_and_payload() -> None:
    product = SimpleNamespace(
        id=1,
        title="Steam Wallet RUB",
        denomination="Variable RUB",
        cost_usd=Decimal("0"),
        supplier_stock="0",
        is_available=False,
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"requires_manual_cost":true}'
        ),
    )

    manage._apply_manual_recharge_config_to_product(
        product,
        quantity=Decimal("1000"),
        cost_usd=Decimal("9.50"),
    )

    assert product.cost_usd == Decimal("9.50")
    assert product.supplier_stock == "unlimited"
    assert product.is_available is True
    assert product.denomination == "Variable RUB | Quantity 1000"
    assert '"recharge_default_params":{"Quantity":"1000"}' in product.raw_payload_json
    assert '"recharge_quantity_source":"fixed"' in product.raw_payload_json
    assert '"requires_manual_cost":false' in product.raw_payload_json


def test_apply_manual_recharge_config_supports_marketplace_quantity_mode() -> None:
    product = SimpleNamespace(
        id=1,
        title="Steam Wallet RUB",
        denomination="Variable RUB | Quantity 90",
        cost_usd=Decimal("6.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"recharge_default_params":{"Quantity":"90"},'
            '"requires_manual_cost":false}'
        ),
    )

    manage._apply_manual_recharge_config_to_product(
        product,
        quantity=Decimal("1"),
        cost_usd=Decimal("0.06"),
        quantity_source="marketplace_quantity",
    )

    assert product.cost_usd == Decimal("0.06")
    assert product.denomination == "Variable RUB | Marketplace quantity x 1"
    assert '"recharge_quantity_source":"marketplace_quantity"' in product.raw_payload_json
    assert '"recharge_quantity_multiplier":"1"' in product.raw_payload_json
    assert "recharge_default_params" not in product.raw_payload_json


def test_title_from_slug_formats_readable_title() -> None:
    assert (
        manage._title_from_slug("playstation-store-wallet-turkey")
        == "Playstation Store Wallet Turkey"
    )


def test_extract_denomination_signature_normalizes_tl_to_try() -> None:
    assert manage._extract_denomination_signature("Playstation 500 TL") == "500 TRY"


def test_extract_denomination_signature_handles_confusable_letters() -> None:
    # First letter is Cyrillic "Т" (U+0422), not Latin "T".
    assert manage._extract_denomination_signature("Playstation 500 \u0422RY") == "500 TRY"


def test_extract_denomination_signature_handles_currency_before_amount() -> None:
    assert manage._extract_denomination_signature("Playstation TRY 500") == "500 TRY"
    assert manage._extract_denomination_signature("Playstation TR 500") == "500 TRY"


def test_auto_match_products_and_snapshots_by_signature() -> None:
    products = [
        SimpleNamespace(
            id=1,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
            supplier_product_id="5",
            raw_payload_json="{}",
            cost_usd="5.50",
            supplier_stock="300+",
        ),
        SimpleNamespace(
            id=2,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="500 TRY",
            supplier_product_id="6",
            raw_payload_json="{}",
            cost_usd="11.00",
            supplier_stock="300+",
        ),
    ]
    snapshots = [
        {"marketplace_offer_id": "5850465", "title": "PSN 250 TRY", "denomination": "250 TRY"},
        {"marketplace_offer_id": "5850466", "title": "PSN 500 TRY", "denomination": "500 TRY"},
    ]

    matches, unmatched_products, unmatched_snapshots, ambiguous = (
        manage._auto_match_products_and_snapshots(products, snapshots)
    )

    assert len(matches) == 2
    assert unmatched_products == []
    assert unmatched_snapshots == []
    assert ambiguous == []


def test_extract_marketplace_snapshot_fields_reads_option_variants() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "name": "Playstation testing",
            "price": 7.0,
            "currency": "USD",
            "options": [
                {
                    "label": "250 TL",
                    "variants": [
                        {"text": "250 TL", "default": 1},
                        {"text": "500 TL", "default": 0},
                    ],
                }
            ],
        }
    )

    assert snapshot["denomination"] == "250 TL"
    assert snapshot["option_variants"] == ["250 TL", "500 TL"]


def test_extract_marketplace_snapshot_fields_reads_ggsel_seller_goods_names() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "id_goods": 102249445,
            "name_goods": "Steam top up",
            "price": 7.0,
            "currency": "USD",
            "visible": 1,
        }
    )

    assert snapshot["title"] == "Steam top up"
    assert snapshot["price"] == 7.0
    assert snapshot["currency"] == "USD"
    assert snapshot["is_enabled"] is True


def test_extract_marketplace_snapshot_fields_detects_required_text_input() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "name": "Steam gift",
            "options": [
                {"label": "Steam friend code", "type": "text", "required": 1},
            ],
        }
    )

    assert snapshot["denomination"] is None
    assert snapshot["detected_requires_buyer_input"] is True
    assert snapshot["detected_buyer_input_label"] == "Steam friend code"


def test_extract_marketplace_snapshot_fields_handles_mixed_options() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "name": "Playstation testing",
            "options": [
                {
                    "label": "Gift Card Amount",
                    "type": "radio",
                    "variants": [
                        {"text": "250 TRY", "default": 1},
                        {"text": "500 TRY", "default": 0},
                    ],
                },
                {
                    "label": "Steam friend code",
                    "type": "text",
                    "required": 1,
                },
            ],
        }
    )

    assert snapshot["option_variant_entries"] == [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY"},
        {"option_label": "Gift Card Amount", "variant_value": "500 TRY"},
    ]
    assert snapshot["detected_requires_buyer_input"] is True
    assert snapshot["detected_buyer_input_label"] == "Steam friend code"


def test_extract_marketplace_snapshot_fields_keeps_variant_ids() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "name": "Playstation testing",
            "options": [
                {
                    "label": "Gift Card Amount",
                    "variants": [
                        {"variant_id": 1001, "text": "250 TRY"},
                        {"variant_id": 1002, "text": "500 TRY"},
                    ],
                }
            ],
        }
    )

    assert snapshot["option_variant_entries"] == [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY", "variant_id": 1001},
        {"option_label": "Gift Card Amount", "variant_value": "500 TRY", "variant_id": 1002},
    ]


def test_extract_marketplace_snapshot_fields_reads_variant_id_from_value_key() -> None:
    snapshot = manage._extract_marketplace_snapshot_fields(
        {
            "name": "Playstation testing",
            "options": [
                {
                    "label": "Gift Card Amount",
                    "variants": [
                        {"text": "250 TRY", "value": "173359"},
                        {"text": "500 TRY", "value": "173360"},
                    ],
                }
            ],
        }
    )

    assert snapshot["option_variant_entries"] == [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY", "variant_id": 173359},
        {"option_label": "Gift Card Amount", "variant_value": "500 TRY", "variant_id": 173360},
    ]


def test_products_matching_variant_uses_denomination_signature() -> None:
    products = [
        SimpleNamespace(
            id=10,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
        ),
        SimpleNamespace(
            id=11,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="500 TRY",
        ),
    ]

    matches = manage._products_matching_variant(products, "500 TL")

    assert [product.id for product in matches] == [11]


def test_products_matching_variant_exact_ignores_fuzzy_results() -> None:
    products = [
        SimpleNamespace(
            id=10,
            title="Google Gift Card",
            category="Gift Card, Google",
            denomination="500 TRY",
        ),
        SimpleNamespace(
            id=11,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
        ),
    ]

    matches = manage._products_matching_variant_exact(products, "Playstation")

    assert matches == []


def test_auto_match_option_entries_matches_gengine_gift_regions() -> None:
    products = [
        SimpleNamespace(
            id=10,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="UA",
            region="UA",
            supplier_product_id="gift:1238840:432909:UA",
            raw_payload_json='{"source_type":"gift","gift_region":"UA"}',
        ),
        SimpleNamespace(
            id=11,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="KZ",
            region="KZ",
            supplier_product_id="gift:1238840:432909:KZ",
            raw_payload_json='{"source_type":"gift","gift_region":"KZ"}',
        ),
        SimpleNamespace(
            id=12,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="CIS",
            region="CIS",
            supplier_product_id="gift:1238840:432909:CIS",
            raw_payload_json='{"source_type":"gift","gift_region":"CIS"}',
        ),
        SimpleNamespace(
            id=13,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="US",
            region="US",
            supplier_product_id="gift:1238840:432909:US",
            raw_payload_json='{"source_type":"gift","gift_region":"US"}',
        ),
    ]
    option_entries = [
        {"option_label": "Country", "variant_value": "UA"},
        {"option_label": "Country", "variant_value": "KZ"},
        {"option_label": "Country", "variant_value": "CIS"},
        {"option_label": "Country", "variant_value": "US"},
    ]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(
        products,
        option_entries,
    )

    assert [(item["entry"]["variant_value"], item["product"].id) for item in matches] == [
        ("UA", 10),
        ("KZ", 11),
        ("CIS", 12),
        ("US", 13),
    ]
    assert unmatched == []
    assert ambiguous == []


def test_auto_match_option_entries_matches_gengine_ru_cis_gift_region_alias() -> None:
    products = [
        SimpleNamespace(
            id=12,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="RU+CIS",
            region="RU+CIS",
            supplier_product_id="gift:1238840:432909:RU-CIS",
            raw_payload_json='{"source_type":"gift","gift_region":"RU+CIS"}',
        ),
    ]
    option_entries = [{"option_label": "Country", "variant_value": "CIS"}]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(
        products,
        option_entries,
    )

    assert [(item["entry"]["variant_value"], item["product"].id) for item in matches] == [
        ("CIS", 12),
    ]
    assert unmatched == []
    assert ambiguous == []


def test_auto_match_option_entries_matches_cis_to_gengine_ge_region_with_cis_zone() -> None:
    products = [
        SimpleNamespace(
            id=12,
            title="Battlefield 1 Revolution",
            category="Games, Steam Gifts",
            denomination="ge",
            region="ge",
            supplier_product_id="gift:1238840:432909:ge",
            raw_payload_json='{"source_type":"gift","gift_region":"ge","zone":"CIS"}',
        ),
    ]
    option_entries = [{"option_label": "Country", "variant_value": "CIS"}]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(
        products,
        option_entries,
    )

    assert [(item["entry"]["variant_value"], item["product"].id) for item in matches] == [
        ("CIS", 12),
    ]
    assert unmatched == []
    assert ambiguous == []


def test_auto_select_single_offer_uses_snapshot_title() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
        SimpleNamespace(
            id=2,
            title="Steam RU+CIS top up",
            category="Games, Steam",
            denomination=None,
            supplier_product_id="719",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
    ]

    product, reason = manage._auto_select_single_offer_product(
        reference,
        products,
        {"title": "Steam RU+CIS direct top up", "denomination": None},
    )

    assert product is not None
    assert product.id == 2
    assert reason == "marketplace title/denomination matched one local row"


def test_auto_select_single_offer_uses_buyer_input_currency_hint() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
        SimpleNamespace(
            id=2,
            title="Steam RUB top up",
            category="Games, Steam",
            denomination=None,
            supplier_product_id="720",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
        SimpleNamespace(
            id=3,
            title="Steam Worldwide",
            category="Games, Steam",
            denomination="Worldwide",
            supplier_product_id="719",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
        SimpleNamespace(
            id=136,
            title="100 PHP",
            category="Gift Card, Steam",
            denomination="100 PHP",
            supplier_product_id="547",
            raw_payload_json='{"recharge_product_id":"1"}',
        ),
    ]

    product, reason = manage._auto_select_single_offer_product(
        reference,
        products,
        {
            "title": "Playstation Testing",
            "denomination": None,
            "option_parameter_names": ["Account (String)", "Quantity (RUB)"],
        },
    )

    assert product is not None
    assert product.id == 2
    assert reason == "marketplace buyer-input currency matched one direct row"


def test_single_offer_manual_candidates_hide_gift_card_rows_for_recharge() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
        ),
        SimpleNamespace(
            id=2,
            title="Steam RUB top up",
            category="Games, Steam",
            denomination=None,
        ),
        SimpleNamespace(
            id=3,
            title="Steam Worldwide",
            category="Games, Steam",
            denomination="Worldwide",
        ),
        SimpleNamespace(
            id=136,
            title="100 PHP",
            category="Gift Card, Steam",
            denomination="100 PHP",
        ),
    ]

    candidates = manage._single_offer_manual_candidates(reference, products)

    assert [product.id for product in candidates] == [2, 3]


def test_should_use_recharge_runtime_option_mapping_for_single_direct_row() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=2,
            title="Steam RUB top up",
            category="Recharge",
            denomination=None,
        )
    ]
    entries = [{"option_label": "Количество", "variant_value": "100 RUB", "variant_id": 19472011}]

    assert manage._should_use_recharge_runtime_option_mapping(reference, products, entries)


def test_should_not_use_recharge_runtime_option_mapping_for_multiple_rows() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=2,
            title="Steam RUB top up",
            category="Recharge",
            denomination=None,
        ),
        SimpleNamespace(
            id=3,
            title="Steam USD top up",
            category="Recharge",
            denomination=None,
        ),
    ]
    entries = [{"option_label": "Количество", "variant_value": "100 RUB", "variant_id": 19472011}]

    assert not manage._should_use_recharge_runtime_option_mapping(reference, products, entries)


def test_auto_select_single_offer_recharge_does_not_auto_select_key_like_row() -> None:
    reference = manage.GEngineProductReference(
        kind="recharge",
        product_id="1",
        slug="steam",
        source="https://b2b.g-engine.net/en/recharge/1/steam",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json='{"recharge_product_id":"1"}',
        )
    ]

    product, reason = manage._auto_select_single_offer_product(
        reference,
        products,
        {"title": "STEAM RUB", "denomination": None},
    )

    assert product is None
    assert reason is None


def test_auto_select_single_offer_uses_snapshot_denomination_signature() -> None:
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="playstation-store-wallet-turkey",
        source="link",
    )
    products = [
        SimpleNamespace(
            id=142,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
            supplier_product_id="5",
            raw_payload_json='{"base_product_id":"5"}',
        ),
        SimpleNamespace(
            id=143,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="500 TRY",
            supplier_product_id="6",
            raw_payload_json='{"base_product_id":"5"}',
        ),
    ]

    product, reason = manage._auto_select_single_offer_product(
        reference,
        products,
        {"title": "Playstation 500 TL", "denomination": None},
    )

    assert product is not None
    assert product.id == 143
    assert reason == "marketplace title/denomination matched one local row"


def test_auto_select_single_offer_narrows_duplicate_amount_by_title() -> None:
    reference = manage.GEngineProductReference(
        kind="shop",
        product_id="5",
        slug="wallet-turkey",
        source="link",
    )
    products = [
        SimpleNamespace(
            id=142,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="500 TRY",
            supplier_product_id="5",
            raw_payload_json='{"base_product_id":"5"}',
        ),
        SimpleNamespace(
            id=143,
            title="Xbox Store Wallet Turkey",
            category="Gift Card, Xbox",
            denomination="500 TRY",
            supplier_product_id="6",
            raw_payload_json='{"base_product_id":"5"}',
        ),
    ]

    product, reason = manage._auto_select_single_offer_product(
        reference,
        products,
        {"title": "Xbox Store Wallet Turkey 500 TL", "denomination": None},
    )

    assert product is not None
    assert product.id == 143
    assert reason == "marketplace title/denomination matched one local row"


def test_auto_select_single_offer_uses_direct_supplier_id() -> None:
    reference = manage.GEngineProductReference(
        kind="unknown",
        product_id="718",
        slug=None,
        source="718",
    )
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RU+CIS",
            category="Games, Steam",
            denomination="Steam Key RU+CIS",
            supplier_product_id="718",
            raw_payload_json="{}",
        ),
        SimpleNamespace(
            id=2,
            title="Steam Worldwide",
            category="Games, Steam",
            denomination="Worldwide",
            supplier_product_id="719",
            raw_payload_json="{}",
        ),
    ]

    product, reason = manage._auto_select_single_offer_product(reference, products, None)

    assert product is not None
    assert product.id == 1
    assert reason == "G-Engine product ID matched one supplier row"


def test_auto_match_option_entries_to_products_matches_exact_signatures() -> None:
    products = [
        SimpleNamespace(
            id=142,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 TRY",
        ),
        SimpleNamespace(
            id=143,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="500 TRY",
        ),
    ]
    entries = [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY"},
        {"option_label": "Gift Card Amount", "variant_value": "500 TRY"},
        {"option_label": "Gift Card Amount", "variant_value": "750 TRY"},
    ]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(products, entries)

    assert [item["product"].id for item in matches] == [142, 143]
    assert [item["entry"]["variant_value"] for item in unmatched] == ["750 TRY"]
    assert ambiguous == []


def test_auto_match_option_entries_to_products_matches_hidden_spaces_and_confusables() -> None:
    products = [
        SimpleNamespace(
            id=142,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250 \u0422RY",
        ),
        SimpleNamespace(
            id=143,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="1\u202f000 TRY",
        ),
    ]
    entries = [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY"},
        {"option_label": "Gift Card Amount", "variant_value": "1000 TRY"},
    ]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(products, entries)

    assert [item["product"].id for item in matches] == [142, 143]
    assert unmatched == []
    assert ambiguous == []


def test_option_entry_description_formats_label_and_value() -> None:
    entry = {"option_label": "Currency", "variant_value": "RUB"}
    assert manage._option_entry_description(entry) == "Currency: RUB"


def test_suggest_products_for_option_entry_falls_back_to_fuzzy_currency_match() -> None:
    products = [
        SimpleNamespace(
            id=1,
            title="Steam RUB top up",
            category="Recharge",
            denomination="Variable RUB",
        ),
        SimpleNamespace(
            id=2,
            title="Steam USD top up",
            category="Recharge",
            denomination="Variable USD",
        ),
    ]
    entry = {"option_label": "Currency", "variant_value": "RUB"}

    suggestions = manage._suggest_products_for_option_entry(products, entry)

    assert [product.id for product in suggestions] == [1]


def test_product_denomination_signature_combines_plain_amount_with_payload_currency() -> None:
    product = SimpleNamespace(
        id=142,
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="250",
        region=None,
        raw_payload_json='{"denomination":250,"currency":"TRY","base_product_id":"5"}',
    )

    assert manage._product_denomination_signature(product) == "250 TRY"


def test_product_denomination_signature_reads_payload_amount_field() -> None:
    product = SimpleNamespace(
        id=142,
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination=None,
        region=None,
        raw_payload_json='{"amount":250,"base_product_id":"5"}',
    )

    assert manage._product_denomination_signature(product) == "250 TRY"


def test_auto_match_option_entries_to_products_combines_plain_amount_with_region_hint() -> None:
    products = [
        SimpleNamespace(
            id=142,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="250",
            region=None,
            raw_payload_json='{"denomination":250,"base_product_id":"5"}',
        ),
        SimpleNamespace(
            id=143,
            title="Playstation Store Wallet Turkey",
            category="Gift Card, PlayStation",
            denomination="750",
            region=None,
            raw_payload_json='{"denomination":750,"base_product_id":"5"}',
        ),
    ]
    entries = [
        {"option_label": "Gift Card Amount", "variant_value": "250 TRY"},
        {"option_label": "Gift Card Amount", "variant_value": "750 TRY"},
    ]

    matches, unmatched, ambiguous = manage._auto_match_option_entries_to_products(products, entries)

    assert [item["product"].id for item in matches] == [142, 143]
    assert unmatched == []
    assert ambiguous == []


def test_suggest_price_for_marketplace_quantity_recharge_uses_simple_rub_formula(
    monkeypatch,
) -> None:
    from app.config import Settings

    monkeypatch.setattr(
        "app.services.fx.resolve_usd_rub_rate",
        lambda _settings: Decimal("75.5273"),
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(
            _env_file=None,
            default_currency="RUB",
            scheduler_enabled=False,
            platform_fee_percent=0.06,
            fx_buffer_percent=0.03,
        ),
    )

    product = SimpleNamespace(
        cost_usd=Decimal("0.0132"),
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity"}'
        ),
    )
    suggested_price = manage._suggest_price(
        product,
        "RUB",
        marketplace="ggsel",
        markup_percent=Decimal("0.15"),
        marketplace_commission_percent=Decimal("0.09"),
        payment_system_commission_percent=Decimal("0.06"),
    )

    assert suggested_price == Decimal("1.236")


def test_suggest_price_for_marketplace_quantity_recharge_uses_simple_usd_formula(
    monkeypatch,
) -> None:
    from app.config import Settings

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(
            _env_file=None,
            default_currency="USD",
            scheduler_enabled=False,
            platform_fee_percent=0.06,
            fx_buffer_percent=0.03,
        ),
    )

    product = SimpleNamespace(
        cost_usd=Decimal("1.00"),
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity"}'
        ),
    )
    suggested_price = manage._suggest_price(
        product,
        "USD",
        marketplace="ggsel",
        markup_percent=Decimal("0.15"),
        marketplace_commission_percent=Decimal("0.09"),
        payment_system_commission_percent=Decimal("0.06"),
    )

    assert suggested_price == Decimal("1.240")


def test_suggest_price_for_standard_product_uses_simple_markup_formula(
    monkeypatch,
) -> None:
    from app.config import Settings

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(
            _env_file=None,
            default_currency="USD",
            scheduler_enabled=False,
            platform_fee_percent=0.06,
            fx_buffer_percent=0.03,
        ),
    )

    product = SimpleNamespace(
        cost_usd=Decimal("1.00"),
        raw_payload_json="{}",
    )
    suggested_price = manage._suggest_price(
        product,
        "USD",
        marketplace="digiseller",
        markup_percent=Decimal("0.15"),
        marketplace_commission_percent=None,
        payment_system_commission_percent=None,
    )

    assert suggested_price == Decimal("1.150")
