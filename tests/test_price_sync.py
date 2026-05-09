from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.clients.base import APIRequestError
from app.config import Settings
from app.db import Base
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct
from app.pricing import calculate_compounded_price, get_markup_percent
from app.services.price_sync import _build_marketplace_clients, sync_offer_prices


@dataclass
class FakeMarketplacePriceClient:
    calls: list[tuple[str, float, str, list[dict[str, Any]] | None]] = field(default_factory=list)
    response: dict[str, Any] | None = None
    error: Exception | None = None
    offer_payload_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    offer_options_by_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def set_offer_price(
        self,
        offer_id: str,
        price: float,
        currency: str,
        *,
        variants: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((offer_id, price, currency, variants))
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return {"status": "ok"}

    def close(self) -> None:
        return None

    def get_offer_by_id(self, offer_id: str) -> dict[str, Any] | None:
        return self.offer_payload_by_id.get(str(offer_id))

    def list_offer_options(self, offer_id: str) -> list[dict[str, Any]]:
        return list(self.offer_options_by_id.get(str(offer_id), []))


@dataclass
class FakeSupplierRefreshClient:
    products_by_id: dict[str, list[dict[str, Any]]]
    error: Exception | None = None

    def list_product_denominations(
        self,
        product_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (fallback_title, source_slug)
        if self.error is not None:
            raise self.error
        return self.products_by_id.get(product_id, [])

    def list_recharge_service_products(
        self,
        service_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
        include_unpriced: bool = False,
    ) -> list[dict[str, Any]]:
        _ = (fallback_title, source_slug, include_unpriced)
        if self.error is not None:
            raise self.error
        return self.products_by_id.get(f"recharge:{service_id}", [])

    def list_gift_app_products(
        self,
        app_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (fallback_title, source_slug)
        if self.error is not None:
            raise self.error
        return self.products_by_id.get(f"gift:{app_id}", [])

    def close(self) -> None:
        return None


def _settings_for_tests(
    *,
    default_currency: Literal["USD", "RUB"] = "USD",
    manual_usd_rub_rate: float | None = None,
) -> Settings:
    kwargs: dict[str, Any] = {}
    if manual_usd_rub_rate is not None:
        kwargs["MANUAL_USD_RUB_RATE"] = manual_usd_rub_rate
    return Settings(
        _env_file=None,
        default_currency=default_currency,
        scheduler_enabled=False,
        **kwargs,
    )


def _db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    yield from _db_session()


def test_price_sync_builds_kingpin_digiseller_client() -> None:
    settings = Settings(
        _env_file=None,
        scheduler_enabled=False,
        DIGISELLER_KINGPIN_API_KEY="kingpin-secret",
        DIGISELLER_KINGPIN_SELLER_ID="987654",
    )

    clients = _build_marketplace_clients(settings)
    try:
        assert set(clients) == {"digiseller_kingpin"}
        client = clients["digiseller_kingpin"]
        assert getattr(client, "api_key") == "kingpin-secret"
        assert getattr(client, "seller_id") == "987654"
    finally:
        for client in clients.values():
            client.close()


def _linked_offer(
    db: Session,
    *,
    marketplace: str = "digiseller",
    marketplace_offer_id: str = "5850465",
    offer_price: Decimal = Decimal("1.00"),
    offer_currency: str = "USD",
    supplier_cost_usd: Decimal = Decimal("11.00"),
    supplier_product_id: str = "500",
    mapping_enabled: bool = True,
    max_price: Decimal | None = None,
    marketplace_commission_percent: Decimal | None = None,
    payment_system_commission_percent: Decimal | None = None,
    raw_payload_json: str = '{"id":"500","product_id":"5","base_product_id":"5"}',
) -> tuple[SupplierProduct, MarketplaceOffer, ProductMapping]:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id=supplier_product_id,
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=supplier_cost_usd,
        supplier_stock="300+",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db.add(supplier)
    db.flush()

    offer = MarketplaceOffer(
        marketplace=marketplace,
        marketplace_offer_id=marketplace_offer_id,
        supplier_product_id=supplier.id,
        title="Playstation Store Wallet Turkey - 500 TRY",
        price=offer_price,
        currency=offer_currency,
        is_enabled=False,
    )
    db.add(offer)
    db.flush()

    mapping = ProductMapping(
        supplier_product_id=supplier.id,
        marketplace_offer_id=offer.id,
        enabled=mapping_enabled,
        max_price=max_price,
        marketplace_commission_percent=marketplace_commission_percent,
        payment_system_commission_percent=payment_system_commission_percent,
    )
    db.add(mapping)
    db.commit()
    return supplier, offer, mapping


def test_price_preview_calculates_recommended_price_without_marketplace_update(
    db_session: Session,
) -> None:
    supplier, offer, _mapping = _linked_offer(db_session)
    client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=True,
        refresh_supplier_prices=False,
    )

    assert result["prices_previewed"] == 1
    assert result["prices_updated"] == 0
    assert client.calls == []
    assert offer.price == Decimal("1.00")
    assert result["details"][0]["action"] == "preview"
    expected_price = calculate_compounded_price(
        supplier.cost_usd,
        markup_percent=get_markup_percent(supplier.cost_usd),
        fee_percent=Decimal("0"),
    )
    assert Decimal(result["details"][0]["recommended_price"]) == expected_price


def test_price_sync_updates_digiseller_rub_price_for_linked_offer(
    db_session: Session,
) -> None:
    supplier, offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1700.00"),
        offer_currency="RUB",
    )
    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="RUB", manual_usd_rub_rate=100.0)

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_rub = calculate_compounded_price(
        supplier.cost_usd * Decimal("100"),
        markup_percent=get_markup_percent(supplier.cost_usd),
        fee_percent=Decimal("0"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_rub
    assert client.calls == [("5850465", float(expected_rub), "RUB", None)]
    assert result["details"][0]["action"] == "updated"


def test_price_sync_uses_zero_default_commission_when_mapping_has_none(
    db_session: Session,
) -> None:
    supplier, offer, _mapping = _linked_offer(
        db_session,
        marketplace="ggsel",
        offer_price=Decimal("12.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("11.00"),
    )
    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="USD")

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"ggsel": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_listed = calculate_compounded_price(
        supplier.cost_usd,
        markup_percent=get_markup_percent(supplier.cost_usd),
        fee_percent=Decimal("0"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_listed
    assert client.calls == [("5850465", float(expected_listed), "USD", None)]
    assert Decimal(result["details"][0]["target_net_price"]) == expected_listed


def test_price_sync_uses_mapping_marketplace_commission_for_ggsel(
    db_session: Session,
) -> None:
    supplier, offer, _mapping = _linked_offer(
        db_session,
        marketplace="ggsel",
        offer_price=Decimal("12.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("11.00"),
        marketplace_commission_percent=Decimal("0.02"),
        payment_system_commission_percent=Decimal("0.01"),
    )
    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="USD")

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"ggsel": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_listed = calculate_compounded_price(
        supplier.cost_usd,
        markup_percent=get_markup_percent(supplier.cost_usd),
        fee_percent=Decimal("0.02"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_listed
    assert client.calls == [("5850465", float(expected_listed), "USD", None)]
    assert result["details"][0]["marketplace_commission_percent"] == "0.02"
    assert result["details"][0]["payment_system_commission_percent"] == "0"


def test_price_sync_skips_when_price_already_matches_recommended(
    db_session: Session,
) -> None:
    expected_price = calculate_compounded_price(
        Decimal("1.00"),
        markup_percent=get_markup_percent(Decimal("1.00")),
        fee_percent=Decimal("0"),
    )
    _supplier, offer, _mapping = _linked_offer(
        db_session,
        marketplace="digiseller",
        offer_price=expected_price,
        offer_currency="USD",
        supplier_cost_usd=Decimal("1.00"),
    )
    client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert client.calls == []
    assert offer.price == expected_price
    assert result["details"][0]["reason"] == "price_unchanged"


def test_price_sync_force_pushes_when_price_matches_recommended(
    db_session: Session,
) -> None:
    expected_price = calculate_compounded_price(
        Decimal("1.00"),
        markup_percent=get_markup_percent(Decimal("1.00")),
        fee_percent=Decimal("0"),
    )
    _supplier, offer, _mapping = _linked_offer(
        db_session,
        marketplace="digiseller",
        offer_price=expected_price,
        offer_currency="USD",
        supplier_cost_usd=Decimal("1.00"),
    )
    client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
        force_marketplace_offer_ids={"5850465"},
    )

    assert result["prices_updated"] == 1
    assert result["skipped"] == 0
    assert client.calls == [("5850465", float(expected_price), "USD", None)]
    assert offer.price == expected_price
    assert result["details"][0]["action"] == "updated"


def test_price_sync_forced_offer_ids_filters_out_other_marketplaces(
    db_session: Session,
) -> None:
    _supplier_a, offer_a, _mapping_a = _linked_offer(
        db_session,
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("11.00"),
        supplier_product_id="500",
    )
    _supplier_b, offer_b, _mapping_b = _linked_offer(
        db_session,
        marketplace="ggsel",
        marketplace_offer_id="102251279",
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("11.00"),
        supplier_product_id="501",
    )
    digiseller_client = FakeMarketplacePriceClient()
    ggsel_client = FakeMarketplacePriceClient(error=RuntimeError("should not be called"))

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={
            "digiseller": digiseller_client,
            "ggsel": ggsel_client,
        },
        dry_run=False,
        refresh_supplier_prices=False,
        force_marketplace_offer_ids={"5850465"},
    )

    expected_price = calculate_compounded_price(
        Decimal("11.00"),
        markup_percent=get_markup_percent(Decimal("11.00")),
        fee_percent=Decimal("0"),
    )
    assert result["offers_checked"] == 1
    assert result["prices_updated"] == 1
    assert result["errors"] == 0
    assert digiseller_client.calls == [("5850465", float(expected_price), "USD", None)]
    assert offer_a.price == expected_price
    assert ggsel_client.calls == []
    assert offer_b.price == Decimal("1.00")


def test_price_sync_skips_when_marketplace_async_task_is_pending(
    db_session: Session,
) -> None:
    _supplier, offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("7.00"),
        offer_currency="USD",
    )
    client = FakeMarketplacePriceClient(response={"status": "pending", "task_id": "task-123"})

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert offer.price == Decimal("7.00")
    assert result["details"][0]["action"] == "skipped"
    assert str(result["details"][0]["reason"]).startswith("marketplace_async_task_pending")


def test_price_sync_disables_inaccessible_digiseller_mapping(
    db_session: Session,
) -> None:
    _supplier, offer, mapping = _linked_offer(
        db_session,
        offer_price=Decimal("7.00"),
        offer_currency="USD",
    )
    client = FakeMarketplacePriceClient(
        error=RuntimeError(
            "Digiseller price update task failed (task-1): "
            "142: Product not found or you do not have edit access"
        )
    )

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["errors"] == 0
    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["reason"] == "marketplace_offer_not_accessible_mapping_disabled"
    assert mapping.enabled is False
    assert offer.price == Decimal("7.00")


def test_price_sync_skips_rub_without_manual_or_auto_rate(db_session: Session) -> None:
    _supplier, offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1700.00"),
        offer_currency="RUB",
    )
    client = FakeMarketplacePriceClient()

    settings = _settings_for_tests(default_currency="RUB")
    settings.auto_usd_rub_rate = False

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert client.calls == []
    assert offer.price == Decimal("1700.00")
    assert "MANUAL_USD_RUB_RATE" in result["details"][0]["reason"]


def test_price_sync_uses_auto_resolved_fx_rate_when_manual_rate_missing(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplier, offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1700.00"),
        offer_currency="RUB",
    )
    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="RUB", manual_usd_rub_rate=None)

    monkeypatch.setattr("app.services.price_sync.resolve_usd_rub_rate", lambda _settings: Decimal("95"))

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_rub = calculate_compounded_price(
        supplier.cost_usd * Decimal("95"),
        markup_percent=get_markup_percent(supplier.cost_usd),
        fee_percent=Decimal("0"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_rub
    assert client.calls == [("5850465", float(expected_rub), "RUB", None)]


def test_price_sync_respects_mapping_max_price(db_session: Session) -> None:
    _supplier, _offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        max_price=Decimal("12.00"),
    )
    client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert client.calls == []
    assert result["details"][0]["reason"] == "recommended_price_exceeds_mapping_max_price"


def test_price_sync_refreshes_linked_gengine_cost_before_preview(
    db_session: Session,
) -> None:
    supplier, _offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("5.50"),
        supplier_product_id="5",
        raw_payload_json='{"id":"5","product_id":"5","base_product_id":"5"}',
    )
    supplier_client = FakeSupplierRefreshClient(
        {
            "5": [
                {
                    "id": "5",
                    "product_id": "5",
                    "base_product_id": "5",
                    "title": "Playstation Store Wallet Turkey",
                    "denomination": "250 TRY",
                    "cost_usd": "6.00",
                    "stock": "300+",
                }
            ]
        }
    )

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": FakeMarketplacePriceClient()},
        supplier_client=supplier_client,
        dry_run=True,
        refresh_supplier_prices=True,
    )

    db_session.refresh(supplier)

    assert result["supplier_refresh_products_checked"] == 1
    assert result["supplier_products_refreshed"] == 1
    assert result["prices_previewed"] == 1
    assert supplier.cost_usd == Decimal("6.00")
    assert Decimal(result["details"][0]["cost_usd"]) == Decimal("6.00")


def test_price_sync_skips_price_push_when_supplier_refresh_is_down(
    db_session: Session,
) -> None:
    supplier, _offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("5.50"),
        supplier_product_id="5",
        raw_payload_json='{"id":"5","product_id":"5","base_product_id":"5"}',
    )
    supplier_client = FakeSupplierRefreshClient(
        {},
        error=APIRequestError("G-Engine returned HTTP 503 for GET /v2.1/shop/products/5"),
    )
    marketplace_client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": marketplace_client},
        supplier_client=supplier_client,
        dry_run=False,
        refresh_supplier_prices=True,
    )

    db_session.refresh(supplier)
    assert result["status"] == "partial"
    assert result["supplier_refresh_errors"] == 1
    assert result["prices_updated"] == 0
    assert result["skipped"] == 1
    assert marketplace_client.calls == []
    assert supplier.cost_usd == Decimal("5.50")
    assert result["details"][0]["reason"] == "supplier_provider_unavailable"
    assert result["details"][1]["reason"] == "supplier_price_refresh_failed"


def test_price_sync_refreshes_recharge_service_cost_before_preview(
    db_session: Session,
) -> None:
    supplier, _offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("0.99"),
        supplier_product_id="recharge:2:10",
        raw_payload_json=(
            '{"id":"recharge:2:10","source_type":"recharge",'
            '"recharge_product_id":"2","recharge_denomination_id":"10"}'
        ),
    )
    supplier_client = FakeSupplierRefreshClient(
        {
            "recharge:2": [
                {
                    "id": "recharge:2:10",
                    "title": "Genshin Impact",
                    "category": "Recharge",
                    "denomination": "60 Genesis Crystals",
                    "cost_usd": "1.25",
                    "stock": "unlimited",
                    "source_type": "recharge",
                    "recharge_product_id": "2",
                    "recharge_denomination_id": "10",
                }
            ]
        }
    )

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": FakeMarketplacePriceClient()},
        supplier_client=supplier_client,
        dry_run=True,
        refresh_supplier_prices=True,
    )

    db_session.refresh(supplier)

    assert result["supplier_refresh_products_checked"] == 1
    assert result["supplier_products_refreshed"] == 1
    assert result["prices_previewed"] == 1
    assert supplier.cost_usd == Decimal("1.25")


def test_price_sync_refreshes_gift_variant_denominations_before_push(
    db_session: Session,
) -> None:
    supplier_ua = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:ua",
        title="Battlefield 1 Revolution",
        category="Games, Steam Gifts",
        denomination="ua",
        region="ua",
        cost_usd=Decimal("40.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"id":"gift:1238840:432909:ua","source_type":"gift",'
            '"gift_app_id":"1238840","gift_package_id":"432909",'
            '"gift_region":"ua","source_slug":"battlefield-1","base_product_id":"1238840"}'
        ),
    )
    supplier_kz = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:kz",
        title="Battlefield 1 Revolution",
        category="Games, Steam Gifts",
        denomination="kz",
        region="kz",
        cost_usd=Decimal("40.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"id":"gift:1238840:432909:kz","source_type":"gift",'
            '"gift_app_id":"1238840","gift_package_id":"432909",'
            '"gift_region":"kz","source_slug":"battlefield-1","base_product_id":"1238840"}'
        ),
    )
    supplier_cis = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:ge",
        title="Battlefield 1 Revolution",
        category="Games, Steam Gifts",
        denomination="ge",
        region="ge",
        cost_usd=Decimal("40.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"id":"gift:1238840:432909:ge","source_type":"gift",'
            '"gift_app_id":"1238840","gift_package_id":"432909",'
            '"gift_region":"ge","gift_zone":"CIS",'
            '"source_slug":"battlefield-1","base_product_id":"1238840"}'
        ),
    )
    db_session.add_all([supplier_ua, supplier_kz, supplier_cis])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102260467",
        supplier_product_id=supplier_ua.id,
        title="Battlefield Test",
        price=Decimal("4000.00"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add_all(
        [
            ProductMapping(
                supplier_product_id=supplier_ua.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=19224613,
                marketplace_variant_name="UA",
            ),
            ProductMapping(
                supplier_product_id=supplier_kz.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=19224614,
                marketplace_variant_name="KZ",
            ),
            ProductMapping(
                supplier_product_id=supplier_cis.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=19224615,
                marketplace_variant_name="CIS",
            ),
        ]
    )
    db_session.commit()

    supplier_client = FakeSupplierRefreshClient(
        {
            "gift:1238840": [
                {
                    "id": "gift:1238840:432909:ua",
                    "title": "Battlefield 1 Revolution",
                    "category": "Games, Steam Gifts",
                    "denomination": "ua",
                    "region": "ua",
                    "cost_usd": "1.496",
                    "stock": "unlimited",
                    "source_type": "gift",
                    "gift_app_id": "1238840",
                    "gift_package_id": "432909",
                    "gift_region": "ua",
                    "base_product_id": "1238840",
                },
                {
                    "id": "gift:1238840:432909:kz",
                    "title": "Battlefield 1 Revolution",
                    "category": "Games, Steam Gifts",
                    "denomination": "kz",
                    "region": "kz",
                    "cost_usd": "1.859",
                    "stock": "unlimited",
                    "source_type": "gift",
                    "gift_app_id": "1238840",
                    "gift_package_id": "432909",
                    "gift_region": "kz",
                    "base_product_id": "1238840",
                },
                {
                    "id": "gift:1238840:432909:ge",
                    "title": "Battlefield 1 Revolution",
                    "category": "Games, Steam Gifts",
                    "denomination": "ge",
                    "region": "ge",
                    "cost_usd": "2.189",
                    "stock": "unlimited",
                    "source_type": "gift",
                    "gift_app_id": "1238840",
                    "gift_package_id": "432909",
                    "gift_region": "ge",
                    "gift_zone": "CIS",
                    "base_product_id": "1238840",
                },
            ]
        }
    )
    marketplace_client = FakeMarketplacePriceClient()

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"ggsel": marketplace_client},
        supplier_client=supplier_client,
        dry_run=False,
        refresh_supplier_prices=True,
    )

    db_session.refresh(supplier_ua)
    db_session.refresh(supplier_kz)
    db_session.refresh(supplier_cis)

    expected_ua = calculate_compounded_price(
        Decimal("1.496"),
        markup_percent=get_markup_percent(Decimal("1.496")),
        fee_percent=Decimal("0"),
    )
    expected_kz = calculate_compounded_price(
        Decimal("1.859"),
        markup_percent=get_markup_percent(Decimal("1.859")),
        fee_percent=Decimal("0"),
    )
    expected_cis = calculate_compounded_price(
        Decimal("2.189"),
        markup_percent=get_markup_percent(Decimal("2.189")),
        fee_percent=Decimal("0"),
    )

    assert result["supplier_refresh_products_checked"] == 1
    assert result["supplier_products_refreshed"] == 3
    assert result["prices_updated"] == 3
    assert supplier_ua.cost_usd == Decimal("1.496")
    assert supplier_kz.cost_usd == Decimal("1.859")
    assert supplier_cis.cost_usd == Decimal("2.189")
    assert offer.price == expected_ua
    assert len(marketplace_client.calls) == 1
    offer_id, base_price, currency, variants = marketplace_client.calls[0]
    assert offer_id == "102260467"
    assert Decimal(str(base_price)) == expected_ua
    assert currency == "USD"
    assert variants is not None
    variant_rates = {int(item["variant_id"]): Decimal(str(item["rate"])) for item in variants}
    assert variant_rates == {
        19224613: Decimal("0.0"),
        19224614: (expected_kz - expected_ua).quantize(Decimal("0.01")),
        19224615: (expected_cis - expected_ua).quantize(Decimal("0.01")),
    }


def test_price_sync_keeps_manual_recharge_cost_when_service_has_no_priced_rows(
    db_session: Session,
) -> None:
    supplier, _offer, _mapping = _linked_offer(
        db_session,
        offer_price=Decimal("1.00"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("9.50"),
        supplier_product_id="recharge:1",
        raw_payload_json=(
            '{"id":"recharge:1","source_type":"recharge",'
            '"recharge_product_id":"1","requires_manual_cost":false,'
            '"recharge_default_params":{"Quantity":"1000"}}'
        ),
    )
    supplier_client = FakeSupplierRefreshClient({"recharge:1": []})

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": FakeMarketplacePriceClient()},
        supplier_client=supplier_client,
        dry_run=True,
        refresh_supplier_prices=True,
    )

    db_session.refresh(supplier)

    assert result["supplier_refresh_skipped"] == 1
    assert result["prices_previewed"] == 1
    assert supplier.cost_usd == Decimal("9.50")
    assert result["details"][0]["reason"] == (
        "recharge_service_has_no_priced_rows_using_existing_cost"
    )


def test_price_sync_uses_simple_rub_formula_for_marketplace_quantity_recharge(
    db_session: Session,
) -> None:
    supplier, offer, mapping = _linked_offer(
        db_session,
        marketplace="ggsel",
        offer_price=Decimal("1.72"),
        offer_currency="RUB",
        supplier_cost_usd=Decimal("0.0132"),
        supplier_product_id="recharge:1",
        raw_payload_json=(
            '{"id":"recharge:1","source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    mapping.min_margin_percent = Decimal("0.15")
    mapping.marketplace_commission_percent = Decimal("0.09")
    mapping.payment_system_commission_percent = Decimal("0.06")
    db_session.add(mapping)
    db_session.commit()

    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="RUB", manual_usd_rub_rate=75.5273)

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"ggsel": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_rub = calculate_compounded_price(
        supplier.cost_usd * Decimal("75.5273"),
        markup_percent=Decimal("0.15"),
        fee_percent=Decimal("0.09"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_rub
    assert client.calls == [("5850465", float(expected_rub), "RUB", None)]
    assert result["details"][0]["marketplace_commission_percent"] == "0.09"
    assert result["details"][0]["payment_system_commission_percent"] == "0"


def test_price_sync_uses_simple_usd_formula_for_marketplace_quantity_recharge(
    db_session: Session,
) -> None:
    _supplier, offer, mapping = _linked_offer(
        db_session,
        marketplace="ggsel",
        offer_price=Decimal("1.72"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("1.00"),
        supplier_product_id="recharge:1",
        raw_payload_json=(
            '{"id":"recharge:1","source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    mapping.min_margin_percent = Decimal("0.15")
    mapping.marketplace_commission_percent = Decimal("0.09")
    mapping.payment_system_commission_percent = Decimal("0.06")
    db_session.add(mapping)
    db_session.commit()

    client = FakeMarketplacePriceClient()
    settings = _settings_for_tests(default_currency="USD")

    result = sync_offer_prices(
        db=db_session,
        settings=settings,
        marketplace_clients={"ggsel": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_usd = calculate_compounded_price(
        Decimal("1.00"),
        markup_percent=Decimal("0.15"),
        fee_percent=Decimal("0.09"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_usd
    assert client.calls == [("5850465", float(expected_usd), "USD", None)]
    assert result["details"][0]["marketplace_commission_percent"] == "0.09"
    assert result["details"][0]["payment_system_commission_percent"] == "0"


def test_price_sync_updates_digiseller_variant_group_with_single_api_call(
    db_session: Session,
) -> None:
    supplier_250 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="5",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        region=None,
        cost_usd=Decimal("5.50"),
        supplier_stock="300+",
        is_available=True,
        raw_payload_json='{"id":"5","product_id":"5","base_product_id":"5"}',
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("11.00"),
        supplier_stock="300+",
        is_available=True,
        raw_payload_json='{"id":"6","product_id":"5","base_product_id":"5"}',
    )
    db_session.add(supplier_250)
    db_session.add(supplier_500)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_250.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=False,
    )
    db_session.add(offer)
    db_session.flush()

    mapping_250 = ProductMapping(
        supplier_product_id=supplier_250.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=1001,
        marketplace_variant_name="250 TRY",
    )
    mapping_500 = ProductMapping(
        supplier_product_id=supplier_500.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=1002,
        marketplace_variant_name="500 TRY",
    )
    db_session.add(mapping_250)
    db_session.add(mapping_500)
    db_session.commit()

    client = FakeMarketplacePriceClient()
    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 2
    assert len(client.calls) == 1
    offer_id, base_price, currency, variants = client.calls[0]
    assert offer_id == "5850465"
    assert currency == "USD"
    assert variants is not None
    variant_ids = {item["variant_id"] for item in variants}
    assert variant_ids == {1001, 1002}
    variant_rates = {int(item["variant_id"]): Decimal(str(item["rate"])) for item in variants}
    assert variant_rates[1001] == Decimal("0.0")
    assert variant_rates[1002] > Decimal("0")
    assert base_price > 0


def test_price_sync_updates_ggsel_variant_group_with_single_api_call(
    db_session: Session,
) -> None:
    supplier_250 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="5",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        region=None,
        cost_usd=Decimal("5.50"),
        supplier_stock="300+",
        is_available=True,
        raw_payload_json='{"id":"5","product_id":"5","base_product_id":"5"}',
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("11.00"),
        supplier_stock="300+",
        is_available=True,
        raw_payload_json='{"id":"6","product_id":"5","base_product_id":"5"}',
    )
    db_session.add_all([supplier_250, supplier_500])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_250.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=False,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add_all(
        [
            ProductMapping(
                supplier_product_id=supplier_250.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=19472011,
                marketplace_variant_name="250 TRY",
            ),
            ProductMapping(
                supplier_product_id=supplier_500.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=19472020,
                marketplace_variant_name="500 TRY",
            ),
        ]
    )
    db_session.commit()

    client = FakeMarketplacePriceClient()
    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"ggsel": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    assert result["prices_updated"] == 2
    assert len(client.calls) == 1
    offer_id, base_price, currency, variants = client.calls[0]
    assert offer_id == "5850465"
    assert currency == "USD"
    assert variants is not None
    assert {item["variant_id"] for item in variants} == {19472011, 19472020}
    assert base_price > 0
    variant_rates = {int(item["variant_id"]): Decimal(str(item["rate"])) for item in variants}
    assert variant_rates[19472011] == Decimal("0.0")
    assert variant_rates[19472020] > Decimal("0")
    details_by_variant = {
        item["variant_id"]: item
        for item in result["details"]
        if isinstance(item, dict) and item.get("variant_id") is not None
    }
    assert details_by_variant[19472011]["variant_price_modifier"] == "0.0"
    assert Decimal(details_by_variant[19472020]["variant_price_modifier"]) > Decimal("0")


def test_price_sync_updates_digiseller_runtime_quantity_variants(
    db_session: Session,
) -> None:
    supplier, offer, mapping = _linked_offer(
        db_session,
        marketplace="digiseller",
        marketplace_offer_id="5856532",
        offer_price=Decimal("0.29"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("0.0132"),
        supplier_product_id="recharge:1",
        raw_payload_json=(
            '{"id":"recharge:1","source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    mapping.min_margin_percent = Decimal("0.25")
    mapping.marketplace_commission_percent = Decimal("0.03")
    db_session.add(mapping)
    db_session.commit()

    client = FakeMarketplacePriceClient(
        offer_payload_by_id={
            "5856532": {
                "id": 5856532,
                "name": "Steam RUB",
                "options": [
                    {
                        "label": "Количество",
                        "variants": [
                            {"id": 19472011, "text": "100 RUB"},
                            {"id": 19472012, "text": "200 RUB"},
                            {"id": 19472013, "text": "5000 RUB"},
                        ],
                    }
                ],
            }
        }
    )

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_100 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("100"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )
    expected_200 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("200"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )
    expected_5000 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("5000"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_100
    assert len(client.calls) == 1
    offer_id, base_price, currency, variants = client.calls[0]
    assert offer_id == "5856532"
    assert currency == "USD"
    assert Decimal(str(base_price)) == expected_100
    assert variants == [
        {"variant_id": 19472011, "rate": 0.0, "type": 2},
        {
            "variant_id": 19472012,
            "rate": float((expected_200 - expected_100).quantize(Decimal("0.01"))),
            "type": 2,
        },
        {
            "variant_id": 19472013,
            "rate": float((expected_5000 - expected_100).quantize(Decimal("0.01"))),
            "type": 2,
        },
    ]
    assert result["details"][0]["runtime_variant_mode"] is True
    assert result["details"][0]["runtime_variant_count"] == 3


def test_price_sync_runtime_quantity_falls_back_to_option_api(
    db_session: Session,
) -> None:
    supplier, offer, mapping = _linked_offer(
        db_session,
        marketplace="digiseller",
        marketplace_offer_id="5856532",
        offer_price=Decimal("0.29"),
        offer_currency="USD",
        supplier_cost_usd=Decimal("0.0132"),
        supplier_product_id="recharge:1",
        raw_payload_json=(
            '{"id":"recharge:1","source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    mapping.min_margin_percent = Decimal("0.25")
    mapping.marketplace_commission_percent = Decimal("0.03")
    db_session.add(mapping)
    db_session.commit()

    client = FakeMarketplacePriceClient(
        offer_payload_by_id={
            "5856532": {
                "id": 5856532,
                "name": "Steam RUB",
            }
        },
        offer_options_by_id={
            "5856532": [
                {
                    "label": "Количество",
                    "variants": [
                        {"id": 19472011, "text": "100 RUB"},
                        {"id": 19472012, "text": "200 RUB"},
                        {"id": 19472013, "text": "5000 RUB"},
                    ],
                }
            ]
        },
    )

    result = sync_offer_prices(
        db=db_session,
        settings=_settings_for_tests(default_currency="USD"),
        marketplace_clients={"digiseller": client},
        dry_run=False,
        refresh_supplier_prices=False,
    )

    expected_100 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("100"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )
    expected_200 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("200"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )
    expected_5000 = calculate_compounded_price(
        Decimal("0.0132") * Decimal("5000"),
        markup_percent=Decimal("0.25"),
        fee_percent=Decimal("0.03"),
    )

    assert result["prices_updated"] == 1
    assert offer.price == expected_100
    assert len(client.calls) == 1
    offer_id, base_price, currency, variants = client.calls[0]
    assert offer_id == "5856532"
    assert currency == "USD"
    assert Decimal(str(base_price)) == expected_100
    assert variants == [
        {"variant_id": 19472011, "rate": 0.0, "type": 2},
        {
            "variant_id": 19472012,
            "rate": float((expected_200 - expected_100).quantize(Decimal("0.01"))),
            "type": 2,
        },
        {
            "variant_id": 19472013,
            "rate": float((expected_5000 - expected_100).quantize(Decimal("0.01"))),
            "type": 2,
        },
    ]
    assert result["details"][0]["runtime_variant_mode"] is True
    assert result["details"][0]["runtime_variant_count"] == 3
