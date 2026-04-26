from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import Base
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct
from app.pricing import calculate_final_price_usd, convert_usd_to_currency
from app.services.price_sync import sync_offer_prices


@dataclass
class FakeMarketplacePriceClient:
    calls: list[tuple[str, float, str, list[dict[str, Any]] | None]] = field(default_factory=list)
    response: dict[str, Any] | None = None

    def set_offer_price(
        self,
        offer_id: str,
        price: float,
        currency: str,
        *,
        variants: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((offer_id, price, currency, variants))
        if self.response is not None:
            return self.response
        return {"status": "ok"}

    def close(self) -> None:
        return None


@dataclass
class FakeSupplierRefreshClient:
    products_by_id: dict[str, list[dict[str, Any]]]

    def list_product_denominations(
        self,
        product_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (fallback_title, source_slug)
        return self.products_by_id.get(product_id, [])

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


def _linked_offer(
    db: Session,
    *,
    marketplace: str = "digiseller",
    offer_price: Decimal = Decimal("1.00"),
    offer_currency: str = "USD",
    supplier_cost_usd: Decimal = Decimal("11.00"),
    supplier_product_id: str = "500",
    mapping_enabled: bool = True,
    max_price: Decimal | None = None,
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
        marketplace_offer_id="5850465",
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
    )
    db.add(mapping)
    db.commit()
    return supplier, offer, mapping


def test_price_preview_calculates_recommended_price_without_marketplace_update(
    db_session: Session,
) -> None:
    _supplier, offer, _mapping = _linked_offer(db_session)
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
    assert Decimal(result["details"][0]["recommended_price"]) > Decimal("11.00")


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

    expected_usd = calculate_final_price_usd(
        supplier.cost_usd,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
    ).final_price_usd
    expected_rub = convert_usd_to_currency(expected_usd, "RUB", Decimal("100.0"))

    assert result["prices_updated"] == 1
    assert offer.price == expected_rub
    assert client.calls == [("5850465", float(expected_rub), "RUB", None)]
    assert result["details"][0]["action"] == "updated"


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

    expected_usd = calculate_final_price_usd(
        supplier.cost_usd,
        platform_fee_percent=Decimal(str(settings.platform_fee_percent)),
        fx_buffer_percent=Decimal(str(settings.fx_buffer_percent)),
        risk_buffer_percent=Decimal(str(settings.risk_buffer_percent)),
    ).final_price_usd
    expected_rub = convert_usd_to_currency(expected_usd, "RUB", Decimal("95"))

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
