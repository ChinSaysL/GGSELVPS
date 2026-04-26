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

    def set_offer_enabled(self, offer_id: str, enabled: bool) -> dict[str, Any]:
        self.offer_calls.append((offer_id, enabled))
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
