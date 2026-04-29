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
from app.models import MarketplaceOffer, Order, ProductMapping, SupplierProduct
from app.services.order_sync import process_digiseller_unique_code, sync_orders


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


@dataclass
class FakeSupplierOrderClient:
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    create_response: dict[str, Any] = field(
        default_factory=lambda: {
            "order_id": "ge-1",
            "status": "completed",
            "codes": ["ABCD-EFGH-1234"],
        }
    )
    status_payload: dict[str, Any] = field(
        default_factory=lambda: {"status": "completed", "codes": ["ABCD-EFGH-1234"]}
    )

    def create_order(
        self,
        product_id: str,
        quantity: int = 1,
        *,
        buyer_inputs: dict[str, str] | None = None,
        external_order_id: str | None = None,
        product_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "buyer_inputs": buyer_inputs,
                "external_order_id": external_order_id,
                "product_payload": product_payload,
            }
        )
        return dict(self.create_response)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, **self.status_payload}

    def close(self) -> None:
        return None


@dataclass
class FakeMarketplaceOrderClient:
    sales: list[dict[str, Any]]
    order_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: list[tuple[str, str]] = field(default_factory=list)

    def get_latest_sales(self) -> list[dict[str, Any]]:
        return self.sales

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.order_payloads.get(order_id, {})

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        _ = buyer_id
        self.messages.append((order_id, message))
        return {"status": "ok"}

    def close(self) -> None:
        return None


@dataclass
class FakeDigisellerOrderClient(FakeMarketplaceOrderClient):
    delivered_unique_codes: list[str] = field(default_factory=list)

    def verify_unique_code(self, unique_code: str) -> dict[str, Any]:
        return {
            "retval": 0,
            "content": {
                "inv": 9001,
                "item_id": 5850465,
                "unique_code_state": {"state": 5},
                "unique_code": unique_code,
            },
        }

    def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]:
        self.delivered_unique_codes.append(unique_code)
        return {"retval": 0}


def _add_linked_offer(
    db_session: Session,
    *,
    marketplace: str,
    offer_id: str,
    marketplace_variant_id: int | None = None,
) -> SupplierProduct:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="718",
        title="Steam RU+CIS",
        category="Games, Steam",
        denomination="Steam Key RU+CIS",
        region=None,
        cost_usd=Decimal("3.50"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace=marketplace,
        marketplace_offer_id=offer_id,
        supplier_product_id=supplier.id,
        title="Steam top up",
        price=Decimal("5.00"),
        currency="USD",
        is_enabled=True,
        requires_buyer_input=True,
    )
    db_session.add(offer)
    db_session.flush()

    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=True,
            marketplace_variant_id=marketplace_variant_id,
            marketplace_variant_name="250 TRY" if marketplace_variant_id else None,
        )
    )
    db_session.commit()
    return supplier


def test_ggsel_paid_order_buys_from_gengine_and_delivers_once(db_session: Session) -> None:
    _add_linked_offer(
        db_session,
        marketplace="ggsel",
        offer_id="102249445",
        marketplace_variant_id=18944509,
    )
    sale = {
        "invoice_id": "9001",
        "id_goods": "102249445",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
        "options": [
            {"name": "Amount", "user_data": "250 TRY", "user_data_id": 18944509},
            {"name": "Steam friend code", "user_data": "123456789", "user_data_id": None},
        ],
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9001": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )
    repeat_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert repeat_stats["orders_delivered"] == 0
    assert order.status == "delivered"
    assert order.delivered_code_encrypted_or_masked == "AB**********34"
    assert supplier_client.create_calls == [
        {
            "product_id": "718",
            "quantity": 1,
            "buyer_inputs": {"steam_friend_code": "123456789"},
            "external_order_id": "ggsel:9001",
            "product_payload": {},
        }
    ]
    assert len(marketplace_client.messages) == 2
    purchase_message = marketplace_client.messages[0][1]
    delivery_message = marketplace_client.messages[1][1]
    assert "Thank you for your purchase!" in purchase_message
    assert "Спасибо за вашу покупку!" in purchase_message
    assert "Product: Steam top up - 250 TRY" in purchase_message
    assert "Your gift card has been delivered." in delivery_message
    assert "Ваша подарочная карта доставлена." in delivery_message
    assert "Product: Steam top up - 250 TRY" in delivery_message
    assert "Товар: Steam top up - 250 TRY" in delivery_message
    assert "ABCD-EFGH-1234" in delivery_message


def test_ggsel_paid_recharge_order_delivers_after_shipped_without_code(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"1",'
        '"recharge_param_keys":["Account","Quantity"],'
        '"recharge_default_params":{"Quantity":"1000"}}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam Wallet RUB",
        category="Recharge",
        denomination="Variable RUB",
        region=None,
        cost_usd=Decimal("9.50"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102249445",
        supplier_product_id=supplier.id,
        title="Steam Wallet RUB 1000",
        price=Decimal("12.00"),
        currency="USD",
        is_enabled=True,
        requires_buyer_input=True,
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

    sale = {
        "invoice_id": "9002",
        "id_goods": "102249445",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "12.00",
        "options": [
            {"name": "Steam friend code", "user_data": "123456789", "user_data_id": None}
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7001, "status": "shipped", "supplier_order_id": "recharge:7001"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9002": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert order.supplier_order_id == "recharge:7001"
    assert order.delivered_code_encrypted_or_masked == "fulfilled_without_code"
    assert supplier_client.create_calls == [
        {
            "product_id": "recharge:1",
            "quantity": 1,
            "buyer_inputs": {"steam_friend_code": "123456789"},
            "external_order_id": "ggsel:9002",
            "product_payload": {
                "source_type": "recharge",
                "recharge_product_id": "1",
                "recharge_param_keys": ["Account", "Quantity"],
                "recharge_default_params": {"Quantity": "1000"},
            },
        }
    ]
    assert len(marketplace_client.messages) == 2
    purchase_message = marketplace_client.messages[0][1]
    delivery_message = marketplace_client.messages[1][1]
    assert "will be delivered shortly" in purchase_message
    assert "будет доставлен в ближайшее время" in purchase_message
    assert "Product: Steam Wallet RUB 1000" in purchase_message
    assert "Your product has been delivered successfully." in delivery_message
    assert "Ваш товар был успешно доставлен." in delivery_message
    assert "Product: Steam Wallet RUB 1000" in delivery_message
    assert "Товар: Steam Wallet RUB 1000" in delivery_message


def test_digiseller_recharge_includes_radio_option_values_in_buyer_inputs(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"1",'
        '"recharge_param_keys":["Account","Currency"],'
        '"recharge_default_params":{}}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam Wallet Top Up",
        category="Recharge",
        denomination="Variable",
        region=None,
        cost_usd=Decimal("1.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier.id,
        title="Steam Top Up",
        price=Decimal("2.00"),
        currency="USD",
        is_enabled=True,
        requires_buyer_input=True,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=True,
            marketplace_variant_id=19472011,
            marketplace_variant_name="RUB",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "2.00",
        "options": [
            {"name": "Currency", "user_data": "RUB", "user_data_id": 19472011},
            {"name": "Account", "user_data": "steam_friend_123", "user_data_id": None},
        ],
        "unique_code": "digiseller-9301",
    }

    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7002, "status": "shipped", "supplier_order_id": "recharge:7002"}
    )
    marketplace_client = FakeDigisellerOrderClient(sales=[sale], order_payloads={"9001": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["buyer_inputs"] == {
        "account": "steam_friend_123",
        "currency": "RUB",
    }


def test_digiseller_recharge_uses_quantity_radio_value_and_single_mapping_fallback(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"1",'
        '"recharge_param_keys":["Account","Quantity"],'
        '"recharge_default_params":{}}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam Wallet Top Up",
        category="Recharge",
        denomination="Variable",
        region=None,
        cost_usd=Decimal("1.00"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="digiseller",
        marketplace_offer_id="5850465",
        supplier_product_id=supplier.id,
        title="Steam RUB",
        price=Decimal("0.29"),
        currency="USD",
        is_enabled=True,
        requires_buyer_input=True,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ProductMapping(
            supplier_product_id=supplier.id,
            marketplace_offer_id=offer.id,
            enabled=True,
            marketplace_variant_id=999999,
            marketplace_variant_name="legacy",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "0.29",
        "options": [
            {"name": "Количество", "user_data": "100 RUB", "user_data_id": 19472011},
            {"name": "Account", "user_data": "steam_friend_123", "user_data_id": None},
        ],
        "unique_code": "digiseller-9310",
    }

    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7010, "status": "shipped", "supplier_order_id": "recharge:7010"}
    )
    marketplace_client = FakeDigisellerOrderClient(sales=[sale], order_payloads={"9001": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["buyer_inputs"] == {
        "account": "steam_friend_123",
        "quantity": "100",
    }


def test_ggsel_steam_game_gift_delivery_instructs_friend_request(
    db_session: Session,
) -> None:
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="900",
        title="Helldivers 2 Steam Gift",
        category="Games, Steam",
        denomination="Steam Gift",
        region=None,
        cost_usd=Decimal("20.00"),
        supplier_stock="10",
        is_available=True,
        raw_payload_json="{}",
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102299999",
        supplier_product_id=supplier.id,
        title="Helldivers 2 Steam",
        price=Decimal("25.00"),
        currency="USD",
        is_enabled=True,
        requires_buyer_input=True,
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

    sale = {
        "invoice_id": "9004",
        "id_goods": "102299999",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "25.00",
        "options": [
            {"name": "Steam friend code", "user_data": "123456789", "user_data_id": None}
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"order_id": "ge-gift-1", "status": "completed"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9004": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert order.delivered_code_encrypted_or_masked == "fulfilled_without_code"
    assert len(marketplace_client.messages) == 2
    purchase_message = marketplace_client.messages[0][1]
    delivery_message = marketplace_client.messages[1][1]
    assert "Please wait while we prepare the delivery." in purchase_message
    assert "пока мы подготовим доставку" in purchase_message
    assert "Your game is ready to be delivered!" in delivery_message
    assert "Ваша игра готова к отправке!" in delivery_message
    assert "accept the friend request from our Steam account" in delivery_message
    assert "примите запрос в друзья от нашего аккаунта Steam" in delivery_message
    assert "Product: Helldivers 2 Steam" in delivery_message
    assert "Товар: Helldivers 2 Steam" in delivery_message


def test_ggsel_quantity_based_recharge_order_scales_cost_and_supplier_quantity(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"1",'
        '"recharge_param_keys":["Account","Quantity"],'
        '"recharge_quantity_source":"marketplace_quantity",'
        '"recharge_quantity_multiplier":"1"}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:1",
        title="Steam Wallet RUB",
        category="Recharge",
        denomination="Variable RUB | Marketplace quantity x 1",
        region=None,
        cost_usd=Decimal("0.0600"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102251279",
        supplier_product_id=supplier.id,
        title="STEAM RUB",
        price=Decimal("1.10"),
        currency="RUB",
        is_enabled=True,
        requires_buyer_input=True,
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

    sale = {
        "invoice_id": "9003",
        "id_goods": "102251279",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "110.00",
        "cnt_goods": "100",
        "options": [
            {"name": "Steam friend code", "user_data": "123456789", "user_data_id": None}
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7004, "status": "shipped", "supplier_order_id": "recharge:7004"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9003": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.cost_usd == Decimal("6.0000")
    assert supplier_client.create_calls[0]["quantity"] == 100
    assert supplier_client.create_calls[0]["product_payload"] == {
        "source_type": "recharge",
        "recharge_product_id": "1",
        "recharge_param_keys": ["Account", "Quantity"],
        "recharge_quantity_source": "marketplace_quantity",
        "recharge_quantity_multiplier": "1",
    }


def test_digiseller_order_waits_for_unique_code_then_delivers(db_session: Session) -> None:
    _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(sales=[sale], order_payloads={"9001": sale})

    sync_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    pending_order = db_session.execute(select(Order)).scalars().one()
    pending_status = pending_order.status
    pending_error = pending_order.error_message
    pending_create_calls = list(supplier_client.create_calls)

    delivery_stats = process_digiseller_unique_code(
        unique_code="UNIQUE-CODE-0001",
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
    )
    repeat_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    delivered_order = db_session.execute(select(Order)).scalars().one()

    assert sync_stats["supplier_orders_created"] == 0
    assert sync_stats["orders_delivered"] == 0
    assert pending_status == "pending"
    assert pending_error == "awaiting_digiseller_unique_code"
    assert pending_create_calls == []
    assert delivery_stats["orders_delivered"] == 1
    assert repeat_stats["orders_delivered"] == 0
    assert repeat_stats["skipped"] == 1
    assert delivered_order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["UNIQUE-CODE-0001"]
    assert len(marketplace_client.messages) == 2
    assert "Thank you for your purchase!" in marketplace_client.messages[0][1]
    assert "Спасибо за вашу покупку!" in marketplace_client.messages[0][1]
    assert "ABCD-EFGH-1234" in marketplace_client.messages[1][1]
    assert "Ваша подарочная карта доставлена." in marketplace_client.messages[1][1]
