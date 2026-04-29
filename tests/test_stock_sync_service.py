from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import Base
from app.models import BalanceSnapshot, MarketplaceOffer, ProductMapping, SupplierProduct
from app.services.stock_sync import sync_offer_stock


@dataclass
class FakeSupplierBalanceClient:
    payload: dict[str, Any]

    def get_balance(self) -> dict[str, Any]:
        return self.payload

    def close(self) -> None:
        return None


@dataclass
class FakeDigisellerStockClient:
    offer_calls: list[tuple[str, bool]] = field(default_factory=list)
    variant_calls: list[tuple[str, int, bool]] = field(default_factory=list)
    variant_state_calls: list[tuple[str, int, bool, bool | None]] = field(default_factory=list)
    variant_state_error_ids: set[int] = field(default_factory=set)
    offer_error: Exception | None = None
    offer_payload_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    offer_options_by_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def set_offer_enabled(self, offer_id: str, enabled: bool) -> dict[str, Any]:
        self.offer_calls.append((offer_id, enabled))
        if self.offer_error is not None:
            raise self.offer_error
        return {"status": "ok"}

    def set_offer_variant_visible(
        self,
        offer_id: str,
        variant_id: int,
        visible: bool,
    ) -> dict[str, Any]:
        self.variant_calls.append((offer_id, variant_id, visible))
        return {"status": "ok"}

    def set_offer_variant_state(
        self,
        offer_id: str,
        variant_id: int,
        *,
        visible: bool,
        is_default: bool | None = None,
    ) -> dict[str, Any]:
        if variant_id in self.variant_state_error_ids:
            raise RuntimeError(f"variant {variant_id} update failed")
        self.variant_state_calls.append((offer_id, variant_id, visible, is_default))
        return {"status": "ok"}

    def close(self) -> None:
        return None

    def get_offer_by_id(self, offer_id: str) -> dict[str, Any] | None:
        return self.offer_payload_by_id.get(str(offer_id))

    def list_offer_options(self, offer_id: str) -> list[dict[str, Any]]:
        return list(self.offer_options_by_id.get(str(offer_id), []))


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


def _settings_for_tests() -> Settings:
    return Settings(_env_file=None, scheduler_enabled=False)


def test_stock_sync_controls_digiseller_variants_independently(db_session: Session) -> None:
    supplier_250 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="5",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        region=None,
        cost_usd=Decimal("5.50"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("60.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_250, supplier_500])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_250.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    mapping_250 = ProductMapping(
        supplier_product_id=supplier_250.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=19472011,
        marketplace_variant_name="250 TRY",
    )
    mapping_500 = ProductMapping(
        supplier_product_id=supplier_500.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=19472020,
        marketplace_variant_name="500 TRY",
    )
    db_session.add_all([mapping_250, mapping_500])
    db_session.commit()

    client = FakeDigisellerStockClient()
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"data": {"balance": "50"}}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert stats["offers_enabled"] == 1
    assert stats["offers_disabled"] == 1
    assert offer.is_enabled is True
    assert client.variant_state_calls == [
        ("5850465", 19472011, True, True),
        ("5850465", 19472020, False, False),
    ]
    assert client.offer_calls == [("5850465", True)]

    snapshot = db_session.query(BalanceSnapshot).order_by(BalanceSnapshot.id.desc()).first()
    assert snapshot is not None
    assert snapshot.balance_usd == Decimal("50")


def test_stock_sync_disables_single_offer_when_unfulfillable(db_session: Session) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="7",
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
        region=None,
        cost_usd=Decimal("33.41"),
        supplier_stock="0",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="718",
        supplier_product_id=supplier.id,
        title="Steam RU+CIS",
        price=Decimal("45.00"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    mapping = ProductMapping(
        supplier_product_id=supplier.id,
        marketplace_offer_id=offer.id,
        enabled=True,
    )
    db_session.add(mapping)
    db_session.commit()

    client = FakeDigisellerStockClient()
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "200.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert client.offer_calls == [("718", False)]
    assert offer.is_enabled is False


def test_stock_sync_forced_offer_ids_filters_out_other_marketplaces(db_session: Session) -> None:
    supplier_a = SupplierProduct(
        supplier="gengine",
        supplier_product_id="5",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="250 TRY",
        region=None,
        cost_usd=Decimal("5.50"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_b = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("11.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_a, supplier_b])
    db_session.flush()

    offer_a = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_a.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=False,
    )
    offer_b = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102251279",
        supplier_product_id=supplier_b.id,
        title="Playstation other",
        price=Decimal("14.00"),
        currency="USD",
        is_enabled=False,
    )
    db_session.add_all([offer_a, offer_b])
    db_session.flush()

    db_session.add_all(
        [
            ProductMapping(
                supplier_product_id=supplier_a.id,
                marketplace_offer_id=offer_a.id,
                enabled=True,
            ),
            ProductMapping(
                supplier_product_id=supplier_b.id,
                marketplace_offer_id=offer_b.id,
                enabled=True,
            ),
        ]
    )
    db_session.commit()

    digiseller_client = FakeDigisellerStockClient()
    ggsel_client = FakeDigisellerStockClient(offer_error=RuntimeError("should not be called"))
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "200.00"}),
        marketplace_clients={
            "digiseller": digiseller_client,
            "ggsel": ggsel_client,
        },
        force_marketplace_offer_ids={"5850465"},
    )

    assert stats["offers_checked"] == 1
    assert stats["errors"] == 0
    assert digiseller_client.offer_calls == [("5850465", True)]
    assert ggsel_client.offer_calls == []


def test_stock_sync_disables_digiseller_variant_parent_when_no_variant_is_fulfillable(
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
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("11.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_250, supplier_500])
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

    client = FakeDigisellerStockClient()
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "0.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert stats["offers_enabled"] == 0
    assert stats["offers_disabled"] == 1
    assert client.variant_state_calls == []
    assert client.offer_calls == [("5850465", False)]
    assert offer.is_enabled is False


def test_stock_sync_treats_digiseller_disable_current_status_error_as_unavailable(
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
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("11.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_250, supplier_500])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_250.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=True,
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

    client = FakeDigisellerStockClient(
        offer_error=RuntimeError(
            "Digiseller price update task failed (task-id): 5850465: "
            "The current status of product does not allow the specified action to be applied"
        )
    )
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "0.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert client.offer_calls == [("5850465", False)]
    assert offer.is_enabled is False
    assert any(
        str(item.get("reason", "")).startswith("parent_offer_already_unavailable")
        for item in stats["details"]
        if isinstance(item, dict)
    )


def test_stock_sync_skips_marketplace_call_when_mapping_is_disabled(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="7",
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
        region=None,
        cost_usd=Decimal("33.41"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="142",
        supplier_product_id=supplier.id,
        title="Steam RU+CIS",
        price=Decimal("45.00"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=False,
        )
    )
    db_session.commit()

    client = FakeDigisellerStockClient(offer_error=RuntimeError("should not be called"))
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "200.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert stats["skipped"] == 1
    assert client.offer_calls == []
    assert offer.is_enabled is False
    assert stats["details"][0]["reason"] == "mapping_disabled_no_marketplace_update"


def test_stock_sync_enforces_single_offer_status_even_when_local_state_matches(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="7",
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
        region=None,
        cost_usd=Decimal("33.41"),
        supplier_stock="0",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="718",
        supplier_product_id=supplier.id,
        title="Steam RU+CIS",
        price=Decimal("45.00"),
        currency="USD",
        is_enabled=False,
    )
    db_session.add(offer)
    db_session.flush()

    mapping = ProductMapping(
        supplier_product_id=supplier.id,
        marketplace_offer_id=offer.id,
        enabled=True,
    )
    db_session.add(mapping)
    db_session.commit()

    client = FakeDigisellerStockClient()
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "200.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert stats["offers_disabled"] == 1
    assert client.offer_calls == [("718", False)]


def test_stock_sync_controls_ggsel_variants_instead_of_disabling_whole_offer(
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
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("60.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_250, supplier_500])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102249445",
        supplier_product_id=supplier_250.id,
        title="Psn turkey",
        price=Decimal("610.55"),
        currency="RUB",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add_all(
        [
            ProductMapping(
                supplier_product_id=supplier_250.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=18944509,
                marketplace_variant_name="250 TRY",
            ),
            ProductMapping(
                supplier_product_id=supplier_500.id,
                marketplace_offer_id=offer.id,
                enabled=True,
                marketplace_variant_id=18944510,
                marketplace_variant_name="500 TRY",
            ),
        ]
    )
    db_session.commit()

    client = FakeDigisellerStockClient(
        offer_error=NotImplementedError("GGSEL status updates are not supported")
    )
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "50.00"}),
        marketplace_clients={"ggsel": client},
    )

    assert stats["errors"] == 0
    assert stats["offers_enabled"] == 1
    assert stats["offers_disabled"] == 1
    assert offer.is_enabled is True
    assert client.variant_state_calls == [
        ("102249445", 18944509, True, True),
        ("102249445", 18944510, False, False),
    ]
    assert client.offer_calls == [("102249445", True)]
    assert any(
        item.get("reason", "").startswith("parent_offer_status_not_updated:")
        for item in stats["details"]
        if isinstance(item, dict)
    )


def test_stock_sync_skips_ggsel_managed_quantity_without_stock_api(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:ua",
        title="Battlefield 1",
        category="Games, Steam Gifts",
        denomination="UA",
        region="ua",
        cost_usd=Decimal("1.50"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102260467",
        supplier_product_id=supplier.id,
        title="Battlefield Test",
        price=Decimal("167.60"),
        currency="RUB",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=True,
            manage_stock_quantity=True,
        )
    )
    db_session.commit()

    client = FakeDigisellerStockClient()
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "50.00"}),
        marketplace_clients={"ggsel": client},
    )

    assert stats["errors"] == 0
    assert any(
        item.get("reason") == "stock_quantity_update_not_supported_by_marketplace"
        for item in stats["details"]
        if isinstance(item, dict)
    )


def test_stock_sync_disables_digiseller_parent_when_variant_control_fails(
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
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    supplier_500 = SupplierProduct(
        supplier="gengine",
        supplier_product_id="6",
        title="Playstation Store Wallet Turkey",
        category="Gift Card, PlayStation",
        denomination="500 TRY",
        region=None,
        cost_usd=Decimal("60.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add_all([supplier_250, supplier_500])
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier_250.id,
        title="Playstation testing",
        price=Decimal("7.00"),
        currency="USD",
        is_enabled=True,
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

    client = FakeDigisellerStockClient(variant_state_error_ids={19472020})
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "50.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 1
    assert client.variant_state_calls == [("5850465", 19472011, True, True)]
    assert client.offer_calls == [("5850465", False)]
    assert offer.is_enabled is False


def test_stock_sync_controls_digiseller_runtime_quantity_variants(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam RUB",
        category="Recharge",
        denomination="Variable RUB | Marketplace quantity x 1",
        region=None,
        cost_usd=Decimal("0.0132"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5856532",
        supplier_product_id=supplier.id,
        title="Steam RUB",
        price=Decimal("0.29"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    mapping = ProductMapping(
        supplier_product_id=supplier.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=None,
    )
    db_session.add(mapping)
    db_session.commit()

    client = FakeDigisellerStockClient(
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
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "50.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert client.variant_state_calls == [
        ("5856532", 19472011, True, True),
        ("5856532", 19472012, True, False),
        ("5856532", 19472013, False, False),
    ]
    assert client.offer_calls == [("5856532", True)]
    assert offer.is_enabled is True


def test_stock_sync_runtime_quantity_falls_back_to_option_api(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam RUB",
        category="Recharge",
        denomination="Variable RUB | Marketplace quantity x 1",
        region=None,
        cost_usd=Decimal("0.0132"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=(
            '{"source_type":"recharge","recharge_product_id":"1",'
            '"recharge_quantity_source":"marketplace_quantity","recharge_quantity_multiplier":"1"}'
        ),
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5856532",
        supplier_product_id=supplier.id,
        title="Steam RUB",
        price=Decimal("0.29"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    mapping = ProductMapping(
        supplier_product_id=supplier.id,
        marketplace_offer_id=offer.id,
        enabled=True,
        marketplace_variant_id=None,
    )
    db_session.add(mapping)
    db_session.commit()

    client = FakeDigisellerStockClient(
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
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "50.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert client.variant_state_calls == [
        ("5856532", 19472011, True, True),
        ("5856532", 19472012, True, False),
        ("5856532", 19472013, False, False),
    ]
    assert client.offer_calls == [("5856532", True)]
    assert offer.is_enabled is True


def test_stock_sync_treats_digiseller_status_update_error_as_safe_noop_for_enabled_offer(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="7",
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
        region=None,
        cost_usd=Decimal("1.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="718",
        supplier_product_id=supplier.id,
        title="Steam RU+CIS",
        price=Decimal("2.00"),
        currency="USD",
        is_enabled=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=True,
        )
    )
    db_session.commit()

    client = FakeDigisellerStockClient(
        offer_error=RuntimeError(
            "Digiseller price update task failed (task-id): 718: "
            "The current status of product does not allow the specified action to be applied"
        )
    )
    stats = sync_offer_stock(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_balance_client=FakeSupplierBalanceClient({"balance": "200.00"}),
        marketplace_clients={"digiseller": client},
    )

    assert stats["errors"] == 0
    assert stats["skipped"] == 1
    assert client.offer_calls == [("718", True)]
    assert offer.is_enabled is True
    assert stats["details"][0]["reason"].startswith("offer_status_unchanged:")
