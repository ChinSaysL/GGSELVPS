from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
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


@dataclass
class FakeMarketplaceClient:
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    update_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _next_id: int = 1

    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(payload)
        offer_id = f"offer-{self._next_id}"
        self._next_id += 1
        return {"offer_id": offer_id}

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.update_calls.append((offer_id, payload))
        return {"offer_id": offer_id}


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


def test_product_sync_creates_supplier_offer_and_mapping(db_session: Session) -> None:
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
    marketplace_client = FakeMarketplaceClient()

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        marketplace_name="digiseller",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()
    mappings = db.execute(select(ProductMapping)).scalars().all()

    assert result["products_imported"] == 1
    assert result["offers_created"] == 1
    assert result["mappings_created"] == 1
    assert len(suppliers) == 1
    assert len(offers) == 1
    assert len(mappings) == 1
    assert offers[0].marketplace_offer_id == "offer-1"


def test_product_sync_builds_bilingual_listing_payload(db_session: Session) -> None:
    db = db_session
    supplier_client = FakeSupplierClient(
        products=[
            {
                "id": "202",
                "title": "Steam Wallet 10 USD",
                "category": "Gift Cards",
                "denomination": "10",
                "region": "Global",
                "description": "Auto delivery",
                "cost_usd": "6.00",
                "image_url": "https://cdn.example.com/steam-10.png",
            }
        ]
    )
    marketplace_client = FakeMarketplaceClient()
    settings = Settings(
        _env_file=None,
        default_currency="USD",
        scheduler_enabled=False,
        listing_title_template_en="[AUTO 24/7] Steam Gift Card {denomination} {currency} | {region}",
        listing_title_template_ru="[AUTO 24/7] Steam Gift Card {denomination} {currency} | {region}",
    )

    result = sync_products(
        db=db,
        settings=settings,
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        marketplace_name="digiseller",
    )
    db.commit()

    assert result["offers_created"] == 1
    assert len(marketplace_client.create_calls) == 1

    payload = marketplace_client.create_calls[0]
    assert payload["title"] == "[AUTO 24/7] Steam Gift Card 10 USD | Global"
    assert payload["title_localized"] == [
        {"locale": "en-US", "value": "[AUTO 24/7] Steam Gift Card 10 USD | Global"},
        {"locale": "ru-RU", "value": "[AUTO 24/7] Steam Gift Card 10 USD | Global"},
    ]
    assert payload["description_ru"].startswith("Мгновенная цифровая доставка")
    assert payload["thumbnail_url"] == "https://cdn.example.com/steam-10.png"


def test_product_sync_second_run_updates_existing_offer_without_duplicates(
    db_session: Session,
) -> None:
    db = db_session
    marketplace_client = FakeMarketplaceClient()

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
        marketplace_client=marketplace_client,
        marketplace_name="digiseller",
    )
    first_result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=second_pass_supplier,
        marketplace_client=marketplace_client,
        marketplace_name="digiseller",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()
    mappings = db.execute(select(ProductMapping)).scalars().all()

    assert len(suppliers) == 1
    assert len(offers) == 1
    assert len(mappings) == 1
    assert len(marketplace_client.create_calls) == 1
    assert len(marketplace_client.update_calls) == 1
    assert first_result["offers_updated"] == 1
    assert suppliers[0].title == "PSN Card 20 USD Updated"


def test_product_sync_creates_offer_when_category_is_missing(
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
    marketplace_client = FakeMarketplaceClient()

    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        marketplace_name="ggsel",
    )
    db.commit()

    suppliers = db.execute(select(SupplierProduct)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()

    assert result["products_imported"] == 1
    assert result["skipped"] == 0
    assert result["offers_created"] == 1
    assert len(suppliers) == 1
    assert len(offers) == 1


def test_product_sync_links_mapping_when_offer_already_exists_without_mapping(
    db_session: Session,
) -> None:
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
        title="Old Offer Title",
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
    marketplace_client = FakeMarketplaceClient()
    result = sync_products(
        db=db,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        marketplace_name="digiseller",
    )
    db.commit()

    mappings = db.execute(select(ProductMapping)).scalars().all()
    offers = db.execute(select(MarketplaceOffer)).scalars().all()

    assert result["offers_updated"] == 1
    assert result["mappings_created"] == 1
    assert len(mappings) == 1
    assert len(offers) == 1
    assert offers[0].marketplace_offer_id == "existing-42"
