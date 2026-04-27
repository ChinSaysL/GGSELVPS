from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import Base
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct
from app.services.product_sync import sync_products


@dataclass
class FakeSupplierClient:
    products: list[dict[str, Any]]

    def list_products(self) -> list[dict[str, Any]]:
        return self.products


def _settings_for_tests() -> Settings:
    return Settings(
        _env_file=None,
        default_currency="USD",
        scheduler_enabled=False,
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


def test_product_sync_imports_supplier_products_without_creating_offers(
    db_session: Session,
) -> None:
    db = db_session
    supplier_client = FakeSupplierClient(
        products=[
            {
                "id": "101",
                "title": "Steam Gift Card 10 USD",
                "category": "Gift Cards",
                "description": "Digital code",
                "cost_usd": "5.50",
                "stock": "300+",
                "is_available": True,
            }
        ]
    )

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_name="digiseller",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()
    mappings = db.execute(select(ProductMapping)).scalars().all()

    assert result["products_imported"] == 1
    assert result["supplier_products_created"] == 1
    assert result["offers_created"] == 0
    assert result["offers_updated"] == 0
    assert result["offers_skipped"] == 1
    assert result["manual_link_required"] == 1
    assert result["mappings_created"] == 0
    assert result["skipped"] == 0
    assert len(suppliers) == 1
    assert len(offers) == 0
    assert len(mappings) == 0


def test_product_sync_second_run_updates_supplier_without_touching_offers(
    db_session: Session,
) -> None:
    db = db_session
    first_pass_supplier = FakeSupplierClient(
        products=[
            {
                "id": "777",
                "title": "PSN Card 20 USD",
                "category": "Gift Cards",
                "cost_usd": "11.00",
            }
        ]
    )
    second_pass_supplier = FakeSupplierClient(
        products=[
            {
                "id": "777",
                "title": "PSN Card 20 USD Updated",
                "category": "Gift Cards",
                "cost_usd": "11.50",
            }
        ]
    )

    sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=first_pass_supplier,
        marketplace_name="digiseller",
    )
    second_result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=second_pass_supplier,
        marketplace_name="digiseller",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()
    mappings = db.execute(select(ProductMapping)).scalars().all()

    assert len(suppliers) == 1
    assert len(offers) == 0
    assert len(mappings) == 0
    assert second_result["supplier_products_updated"] == 1
    assert second_result["offers_created"] == 0
    assert second_result["offers_updated"] == 0
    assert second_result["offers_skipped"] == 1
    assert second_result["manual_link_required"] == 1
    assert suppliers[0].title == "PSN Card 20 USD Updated"
    assert suppliers[0].cost_usd == Decimal("11.50")


def test_product_sync_does_not_auto_link_existing_offers(db_session: Session) -> None:
    db = db_session
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="4000",
        title="Precreated Product",
        category="Gift Cards",
        denomination=None,
        region=None,
        cost_usd=Decimal("9.00"),
        supplier_stock="100",
        is_available=True,
        raw_payload_json="{}",
    )
    db.add(supplier)
    db.flush()

    existing_offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="existing-42",
        supplier_product_id=supplier.id,
        title="Existing Manual Offer",
        price=Decimal("12.00"),
        currency="USD",
        is_enabled=False,
    )
    db.add(existing_offer)
    db.commit()

    supplier_client = FakeSupplierClient(
        products=[
            {
                "id": "4000",
                "title": "Precreated Product Updated",
                "category": "Gift Cards",
                "cost_usd": "9.00",
            }
        ]
    )

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_name="digiseller",
    )
    db.commit()

    mappings = db.execute(select(ProductMapping)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()

    assert result["offers_created"] == 0
    assert result["offers_updated"] == 0
    assert result["mappings_created"] == 0
    assert result["manual_link_required"] == 1
    assert len(mappings) == 0
    assert len(offers) == 1
    assert offers[0].marketplace_offer_id == "existing-42"
    assert offers[0].title == "Existing Manual Offer"


def test_product_sync_without_marketplace_key_imports_products_without_link_warning(
    db_session: Session,
) -> None:
    db = db_session
    supplier_client = FakeSupplierClient(
        products=[
            {
                "id": "900",
                "title": "Unknown Product",
                "cost_usd": "3.50",
            }
        ]
    )

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_name=None,
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()

    assert result["products_imported"] == 1
    assert result["manual_link_required"] == 0
    assert result["offers_skipped"] == 0
    assert result["skipped"] == 0
    assert len(suppliers) == 1
    assert len(offers) == 0


def test_product_sync_imports_recharge_metadata_for_exact_link_matching(
    db_session: Session,
) -> None:
    db = db_session
    supplier_client = FakeSupplierClient(
        products=[
            {
                "id": "recharge:2:10",
                "title": "Genshin Impact",
                "category": "Recharge",
                "denomination": "60 Genesis Crystals",
                "cost_usd": "0.99",
                "stock": "unlimited",
                "is_available": True,
                "source_type": "recharge",
                "recharge_product_id": "2",
                "recharge_denomination_id": "10",
            }
        ]
    )

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_name=None,
    )
    db.commit()

    supplier = db.execute(select(SupplierProduct)).scalars().one()

    assert result["products_imported"] == 1
    assert supplier.supplier_product_id == "recharge:2:10"
    assert supplier.title == "Genshin Impact"
    assert supplier.denomination == "60 Genesis Crystals"
    assert supplier.cost_usd == Decimal("0.99")
    assert '"recharge_product_id":"2"' in (supplier.raw_payload_json or "")


def test_product_sync_skips_products_missing_core_fields(db_session: Session) -> None:
    db = db_session
    supplier_client = FakeSupplierClient(
        products=[
            {"id": "missing-title", "cost_usd": "1.00"},
            {"title": "Missing Cost"},
        ]
    )

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_name="ggsel",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()
    mappings = db.execute(select(ProductMapping)).scalars().all()

    assert result["products_received"] == 2
    assert result["products_imported"] == 0
    assert result["skipped"] == 2
    assert result["manual_link_required"] == 0
    assert result["offers_skipped"] == 0
    assert len(suppliers) == 0
    assert len(offers) == 0
    assert len(mappings) == 0
