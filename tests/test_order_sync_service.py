from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.base import APIRequestError
from app.config import Settings
from app.db import Base
from app.models import MarketplaceOffer, Order, ProductMapping, SupplierProduct
from app.services.provider_status import SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED
from app.services.order_sync import _parse_additive_quantity, process_digiseller_unique_code, retry_order, sync_orders


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
    create_error: Exception | None = None
    status_error: Exception | None = None
    status_calls: list[str] = field(default_factory=list)

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
        if self.create_error is not None:
            raise self.create_error
        return dict(self.create_response)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        self.status_calls.append(order_id)
        if self.status_error is not None:
            raise self.status_error
        return {"order_id": order_id, **self.status_payload}

    def close(self) -> None:
        return None


@dataclass
class FakeMarketplaceOrderClient:
    sales: list[dict[str, Any]]
    order_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: list[tuple[str, str]] = field(default_factory=list)

    latest_sales_windows: list[tuple[datetime | None, datetime | None]] = field(default_factory=list)

    def get_latest_sales(
        self,
        *,
        date_start: datetime | None = None,
        date_finish: datetime | None = None,
    ) -> list[dict[str, Any]]:
        self.latest_sales_windows.append((date_start, date_finish))
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
    chat_messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

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

    def get_buyer_messages(self, order_id: str) -> list[dict[str, Any]]:
        return self.chat_messages.get(order_id, [])


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
    assert len(marketplace_client.messages) == 1
    delivery_message = marketplace_client.messages[0][1]
    assert "Your gift card has been delivered." in delivery_message
    assert "Ваша подарочная карта доставлена." in delivery_message
    assert "Product: Steam top up - 250 TRY" in delivery_message
    assert "Товар: Steam top up - 250 TRY" in delivery_message
    assert "ABCD-EFGH-1234" in delivery_message


def test_ggsel_delivery_skips_unique_code_verification(db_session: Session) -> None:
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
        "unique_code": "GGSEL-UNIQUE-0001",
        "options": [
            {"name": "Amount", "user_data": "250 TRY", "user_data_id": 18944509},
            {"name": "Steam friend code", "user_data": "123456789", "user_data_id": None},
        ],
    }

    class GGSELClientWithForbiddenUniqueCode(FakeMarketplaceOrderClient):
        def verify_unique_code(self, unique_code: str) -> dict[str, Any]:
            raise AssertionError(f"GGSEL unique-code verification must not run: {unique_code}")

        def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]:
            raise AssertionError(f"GGSEL unique-code delivery must not run: {unique_code}")

    supplier_client = FakeSupplierOrderClient()
    marketplace_client = GGSELClientWithForbiddenUniqueCode(
        sales=[sale],
        order_payloads={"9001": sale},
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert len(marketplace_client.messages) == 1
    assert "Your gift card has been delivered." in marketplace_client.messages[0][1]


def test_supplier_provider_down_during_purchase_requires_manual_retry(
    db_session: Session,
) -> None:
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
    supplier_down = FakeSupplierOrderClient(
        create_error=APIRequestError("G-Engine request failed: POST /v2.1/shop/orders")
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9001": sale})

    first_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_down,
        marketplace_clients={"ggsel": marketplace_client},
    )
    second_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_down,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert first_stats["status"] == "partial"
    assert first_stats["provider_unavailable"] == 1
    assert first_stats["errors"] == 1
    assert second_stats["provider_unavailable"] == 0
    assert order.status == "needs_manual_review"
    assert order.supplier_order_id is None
    assert str(order.error_message).startswith("supplier_purchase_status_unknown:")
    assert len(marketplace_client.messages) == 1
    assert "Our provider is currently down" in marketplace_client.messages[0][1]

    supplier_up = FakeSupplierOrderClient()
    auto_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_up,
        marketplace_clients={"ggsel": marketplace_client},
    )

    db_session.refresh(order)
    assert auto_stats["orders_delivered"] == 0
    assert supplier_up.create_calls == []
    assert retry_order(order, db_session) == {"status": "queued", "action": "retry_processing"}

    recovered_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_up,
        marketplace_clients={"ggsel": marketplace_client},
    )

    db_session.refresh(order)
    assert recovered_stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert supplier_up.create_calls == [
        {
            "product_id": "718",
            "quantity": 1,
            "buyer_inputs": {"steam_friend_code": "123456789"},
            "external_order_id": "ggsel:9001",
            "product_payload": {},
        }
    ]
    assert len(marketplace_client.messages) == 2
    assert "Your gift card has been delivered." in marketplace_client.messages[1][1]


def test_supplier_provider_down_during_delivery_poll_stays_retryable(
    db_session: Session,
) -> None:
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
    supplier_down = FakeSupplierOrderClient(
        create_response={"order_id": "ge-1", "status": "processing"},
        status_error=APIRequestError("G-Engine returned HTTP 503 for GET /v2.1/shop/orders/ge-1"),
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9001": sale})

    first_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_down,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert first_stats["provider_unavailable"] == 1
    assert order.status == "supplier_order_created"
    assert order.supplier_order_id == "ge-1"
    assert order.error_message == SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED
    assert len(marketplace_client.messages) == 2
    assert "Our provider is currently down" in marketplace_client.messages[1][1]

    supplier_up = FakeSupplierOrderClient()
    recovered_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_up,
        marketplace_clients={"ggsel": marketplace_client},
    )

    db_session.refresh(order)
    assert recovered_stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert supplier_up.create_calls == []
    assert len(marketplace_client.messages) == 3
    assert "Your gift card has been delivered." in marketplace_client.messages[2][1]


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
    assert len(marketplace_client.messages) == 1
    delivery_message = marketplace_client.messages[0][1]
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
    assert len(marketplace_client.messages) == 1
    delivery_message = marketplace_client.messages[0][1]
    assert "Your game is ready to be delivered!" in delivery_message
    assert "Ваша игра готова к отправке!" in delivery_message
    assert "accept the friend request from our Steam account" in delivery_message
    assert "примите запрос в друзья от нашего аккаунта Steam" in delivery_message
    assert "Product: Helldivers 2 Steam" in delivery_message
    assert "Товар: Helldivers 2 Steam" in delivery_message


def test_ggsel_gengine_gift_order_uses_common_supplier_flow(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"gift","gift_app_id":"4451080",'
        '"gift_package_id":"777","gift_region":"RU"}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:4451080:777:RU",
        title="RV There Yet?",
        category="Games, Steam Gifts",
        denomination="RU",
        region="RU",
        cost_usd=Decimal("4.23"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102300001",
        supplier_product_id=supplier.id,
        title="RV There Yet? Steam Gift",
        price=Decimal("7.00"),
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
            marketplace_variant_id=445108001,
            marketplace_variant_name="RU",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9006",
        "id_goods": "102300001",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "7.00",
        "options": [
            {"name": "Country", "user_data": "RU", "user_data_id": 445108001},
            {
                "name": "Quick-invite Steam URL",
                "user_data": "https://steamcommunity.com/invite/abc",
                "user_data_id": None,
            },
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"supplier_order_id": "gift:9001", "status": "delivered"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9006": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert supplier_client.create_calls == [
        {
            "product_id": "gift:4451080:777:RU",
            "quantity": 1,
            "buyer_inputs": {"quick_invite_steam_url": "https://steamcommunity.com/invite/abc"},
            "external_order_id": "ggsel:9006",
            "product_payload": {
                "source_type": "gift",
                "gift_app_id": "4451080",
                "gift_package_id": "777",
                "gift_region": "RU",
            },
        }
    ]
    assert len(marketplace_client.messages) == 1
    assert "Your game is ready to be delivered!" in marketplace_client.messages[0][1]


def test_gengine_gift_delivery_uses_linked_ru_cis_region_for_cis_marketplace_option(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"gift","gift_app_id":"1238840",'
        '"gift_package_id":"432909","gift_region":"RU+CIS"}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:RU-CIS",
        title="Battlefield 1 Revolution",
        category="Games, Steam Gifts",
        denomination="RU+CIS",
        region="RU+CIS",
        cost_usd=Decimal("2.19"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102260467",
        supplier_product_id=supplier.id,
        title="Battlefield Test",
        price=Decimal("100.00"),
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
            marketplace_variant_id=222003,
            marketplace_variant_name="CIS",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9010",
        "id_goods": "102260467",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "100.00",
        "options": [
            {"name": "Country", "user_data": "CIS", "user_data_id": 222003},
            {
                "name": "Quick-Invite Steam URL",
                "user_data": "https://steamcommunity.com/invite/abc",
                "user_data_id": None,
            },
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"supplier_order_id": "gift:9010", "status": "delivered"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9010": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert supplier_client.create_calls == [
        {
            "product_id": "gift:1238840:432909:RU-CIS",
            "quantity": 1,
            "buyer_inputs": {"quick_invite_steam_url": "https://steamcommunity.com/invite/abc"},
            "external_order_id": "ggsel:9010",
            "product_payload": {
                "source_type": "gift",
                "gift_app_id": "1238840",
                "gift_package_id": "432909",
                "gift_region": "RU+CIS",
            },
        }
    ]
    assert len(marketplace_client.messages) == 1
    assert "Your game is ready to be delivered!" in marketplace_client.messages[0][1]


def test_gengine_gift_delivery_uses_ge_api_region_for_cis_marketplace_option(
    db_session: Session,
) -> None:
    raw_payload_json = (
        '{"source_type":"gift","gift_app_id":"1238840",'
        '"gift_package_id":"432909","gift_region":"ge","gift_zone":"CIS"}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="gift:1238840:432909:ge",
        title="Battlefield 1 Revolution",
        category="Games, Steam Gifts",
        denomination="ge",
        region="ge",
        cost_usd=Decimal("2.19"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102260467",
        supplier_product_id=supplier.id,
        title="Battlefield Test",
        price=Decimal("100.00"),
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
            marketplace_variant_id=222003,
            marketplace_variant_name="CIS",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9011",
        "id_goods": "102260467",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "100.00",
        "options": [
            {"name": "Country", "user_data": "CIS", "user_data_id": 222003},
            {
                "name": "Quick-Invite Steam URL",
                "user_data": "https://steamcommunity.com/invite/abc",
                "user_data_id": None,
            },
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"supplier_order_id": "gift:9011", "status": "delivered"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9011": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert supplier_client.create_calls == [
        {
            "product_id": "gift:1238840:432909:ge",
            "quantity": 1,
            "buyer_inputs": {"quick_invite_steam_url": "https://steamcommunity.com/invite/abc"},
            "external_order_id": "ggsel:9011",
            "product_payload": {
                "source_type": "gift",
                "gift_app_id": "1238840",
                "gift_package_id": "432909",
                "gift_region": "ge",
                "gift_zone": "CIS",
            },
        }
    ]


def test_supplier_cancelled_order_needs_manual_review_and_retry_can_recreate(
    db_session: Session,
) -> None:
    _add_linked_offer(db_session, marketplace="ggsel", offer_id="102251279")
    sale = {
        "invoice_id": "9005",
        "id_goods": "102251279",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7473, "status": "pending"},
        status_payload={"status": "canceled"},
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9005": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["errors"] == 1
    assert stats["details"][-1]["reason"] == "supplier_order_terminal:canceled"
    assert order.status == "needs_manual_review"
    assert order.supplier_order_id == "7473"
    assert order.error_message == "supplier_order_terminal:canceled"

    retry_result = retry_order(order, db_session)

    assert retry_result == {"status": "queued", "action": "retry_processing"}
    assert order.status == "pending"
    assert order.supplier_order_id is None
    assert order.error_message is None

    marketplace_client.sales = []
    supplier_client.create_response = {
        "order_id": "ge-retry-1",
        "status": "completed",
        "codes": ["RETRY-CODE-0001"],
    }

    retry_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    db_session.refresh(order)
    assert retry_stats["orders_detected"] == 1
    assert retry_stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert order.supplier_order_id == "ge-retry-1"


def test_manually_cancelled_digiseller_order_is_not_reopened_when_sale_reappears(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="cancelled",
            error_message="manually_cancelled",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[sale],
        order_payloads={"9001": sale},
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "unique code E5E8B7E1369947A3",
                    "buyer": 1,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:01:00",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["supplier_orders_created"] == 0
    assert stats["orders_delivered"] == 0
    assert stats["details"][-1]["reason"] == "order_manually_cancelled"
    assert order.status == "cancelled"
    assert order.error_message == "manually_cancelled"
    assert supplier_client.create_calls == []
    assert supplier_client.status_calls == []
    assert marketplace_client.messages == []
    assert marketplace_client.delivered_unique_codes == []


def test_manually_cancelled_order_with_supplier_id_is_not_polled_or_delivered(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="ggsel", offer_id="102251279")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="ggsel",
            marketplace_order_id="9006",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="cancelled",
            supplier_order_id="ge-1",
            error_message="manually_cancelled",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9006",
        "id_goods": "102251279",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient(
        status_payload={"status": "completed", "codes": ["SHOULD-NOT-DELIVER"]}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9006": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["supplier_orders_created"] == 0
    assert stats["orders_delivered"] == 0
    assert stats["details"][-1]["reason"] == "order_manually_cancelled"
    assert order.status == "cancelled"
    assert order.supplier_order_id == "ge-1"
    assert supplier_client.create_calls == []
    assert supplier_client.status_calls == []
    assert marketplace_client.messages == []


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


def test_digiseller_recharge_amount_option_becomes_gengine_quantity(
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
        title="Steam Wallet USD",
        category="Recharge",
        denomination="Variable USD | Marketplace quantity x 1",
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
        title="Steam USD top up",
        price=Decimal("5.50"),
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
            marketplace_variant_name="$5 USD",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.50",
        "cnt_goods": "1",
        "options": [
            {"name": "Amount", "user_data": "$5 USD", "user_data_id": 19472011},
            {"name": "Account", "user_data": "steam_friend_123", "user_data_id": None},
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 7007, "status": "shipped", "supplier_order_id": "recharge:7007"}
    )
    marketplace_client = FakeDigisellerOrderClient(
        sales=[sale],
        order_payloads={"9001": sale},
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "unique code is E5E8B7E1369947A3",
                    "buyer": 1,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:01:00",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["quantity"] == 1
    assert supplier_client.create_calls[0]["buyer_inputs"] == {
        "account": "steam_friend_123",
        "quantity": "5",
    }
    assert marketplace_client.delivered_unique_codes == ["E5E8B7E1369947A3"]


def test_recharge_order_with_account_and_region_integer_params(
    db_session: Session,
) -> None:
    """GGSEL labels 'Account (Integer)' and 'Region (Integer)' must map to
    G-Engine param keys 'Account' and 'Region' — the type suffix must not
    bloat the key (e.g. 'region_integer' would never match 'region')."""
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"5",'
        '"recharge_denomination_id":"denom1",'
        '"recharge_param_keys":["Account","Region"]}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:5:denom1",
        title="Mobile Legends 35 Diamonds Russia",
        category="Recharge",
        denomination="32 + 3 Diamonds",
        region="Russia",
        cost_usd=Decimal("0.6834"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102270010",
        supplier_product_id=supplier.id,
        title="MLBB Diamonds Russia",
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
            marketplace_variant_id=None,
            marketplace_variant_name="32 + 3 Diamonds",
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9030",
        "id_goods": "102270010",
        "date_pay": "2026-05-01 10:00:00",
        "amount": "2.00",
        "denomination": "35 Diamonds",
        "options": [
            {"name": "Account (Integer)", "user_data": "123456789", "user_data_id": None},
            {"name": "Region (Integer)", "user_data": "5", "user_data_id": None},
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 9001, "status": "shipped", "supplier_order_id": "recharge:9001"}
    )
    marketplace_client = FakeMarketplaceOrderClient(
        sales=[sale], order_payloads={"9030": sale}
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["buyer_inputs"] == {
        "account": "123456789",
        "region": "5",
    }


def test_recharge_order_with_russian_account_and_region_labels(
    db_session: Session,
) -> None:
    """Russian GGSEL labels 'Счёт (целое число)' and 'Регион (целое число)' must
    map to G-Engine param keys 'Account' and 'Region'."""
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"5",'
        '"recharge_denomination_id":"denom1",'
        '"recharge_param_keys":["Account","Region"]}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:5:denom1",
        title="Mobile Legends 35 Diamonds Russia",
        category="Recharge",
        denomination="32 + 3 Diamonds",
        region="Russia",
        cost_usd=Decimal("0.6834"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102270011",
        supplier_product_id=supplier.id,
        title="MLBB Diamonds Russia",
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
        )
    )
    db_session.commit()

    sale = {
        "invoice_id": "9031",
        "id_goods": "102270011",
        "date_pay": "2026-05-01 10:00:00",
        "amount": "2.00",
        "denomination": "35 Diamonds",
        "options": [
            {"name": "Счёт (целое число)", "user_data": "123456789", "user_data_id": None},
            {"name": "Регион (целое число)", "user_data": "5", "user_data_id": None},
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 9002, "status": "shipped", "supplier_order_id": "recharge:9002"}
    )
    marketplace_client = FakeMarketplaceOrderClient(
        sales=[sale], order_payloads={"9031": sale}
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["buyer_inputs"] == {
        "account": "123456789",
        "region": "5",
    }


def test_digiseller_paid_order_waits_until_buyer_posts_unique_code(db_session: Session) -> None:
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
    repeat_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    pending_order = db_session.execute(select(Order)).scalars().one()

    assert sync_stats["supplier_orders_created"] == 0
    assert sync_stats["orders_delivered"] == 0
    assert sync_stats["details"][-1]["reason"] == "awaiting_digiseller_unique_code"
    assert repeat_stats["orders_delivered"] == 0
    assert repeat_stats["skipped"] == 1
    assert pending_order.status == "pending"
    assert pending_order.error_message == "awaiting_digiseller_unique_code;prompt_sent"
    assert len(supplier_client.create_calls) == 0
    assert marketplace_client.delivered_unique_codes == []
    assert len(marketplace_client.messages) == 1
    assert "please paste your unique code" in marketplace_client.messages[0][1]


def test_digiseller_paid_order_prompts_even_when_chat_lookup_fails(
    db_session: Session,
) -> None:
    _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }

    class ChatLookupFailingDigisellerClient(FakeDigisellerOrderClient):
        def get_buyer_messages(self, order_id: str) -> list[dict[str, Any]]:
            _ = order_id
            raise APIRequestError("Digiseller returned HTTP 403 for GET /api/debates/v2")

    supplier_client = FakeSupplierOrderClient()
    marketplace_client = ChatLookupFailingDigisellerClient(
        sales=[sale],
        order_payloads={"9001": sale},
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["supplier_orders_created"] == 0
    assert stats["orders_delivered"] == 0
    assert str(stats["details"][-1]["reason"]).startswith(
        "awaiting_digiseller_unique_code_chat_lookup_failed"
    )
    assert order.status == "pending"
    assert str(order.error_message).startswith("awaiting_digiseller_unique_code_chat_lookup_failed")
    assert str(order.error_message).endswith(";prompt_sent")
    assert supplier_client.create_calls == []
    assert len(marketplace_client.messages) == 1
    assert "please paste your unique code" in marketplace_client.messages[0][1]


def test_existing_pending_digiseller_order_without_prompt_marker_gets_prompted_once(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="pending",
            error_message="awaiting_digiseller_unique_code",
        )
    )
    db_session.commit()

    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[],
        order_payloads={
            "9001": {
                "retval": 0,
                "content": {
                    "inv": 9001,
                    "item_id": 5850465,
                },
            }
        },
    )

    first_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    second_stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert first_stats["orders_detected"] == 1
    assert first_stats["orders_delivered"] == 0
    assert second_stats["orders_delivered"] == 0
    assert order.status == "pending"
    assert order.error_message == "awaiting_digiseller_unique_code;prompt_sent"
    assert supplier_client.create_calls == []
    assert len(marketplace_client.messages) == 1
    assert "please paste your unique code" in marketplace_client.messages[0][1]


def test_digiseller_unique_code_does_not_recreate_cancelled_supplier_order(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="needs_manual_review",
            supplier_order_id="7473",
            error_message="supplier_order_terminal:canceled",
        )
    )
    db_session.commit()

    supplier_client = FakeSupplierOrderClient(
        create_response={
            "order_id": "ge-new-1",
            "status": "completed",
            "codes": ["NEW-CODE-0001"],
        }
    )
    marketplace_client = FakeDigisellerOrderClient(sales=[])

    stats = process_digiseller_unique_code(
        unique_code="UNIQUE-CODE-0004",
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["status"] == "pending"
    assert stats["errors"] == 0
    assert stats["supplier_orders_created"] == 0
    assert stats["orders_delivered"] == 0
    assert stats["details"][-1]["reason"] == "supplier_order_terminal_requires_manual_retry"
    assert order.status == "needs_manual_review"
    assert order.supplier_order_id == "7473"
    assert len(supplier_client.create_calls) == 0
    assert marketplace_client.delivered_unique_codes == []


def test_digiseller_unique_code_does_not_recreate_supplier_order_cancelled_during_status_check(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="supplier_order_created",
            supplier_order_id="7473",
            error_message="supplier_order_not_ready",
        )
    )
    db_session.commit()

    supplier_client = FakeSupplierOrderClient(
        create_response={
            "order_id": "ge-new-2",
            "status": "completed",
            "codes": ["NEW-CODE-0002"],
        },
        status_payload={"status": "canceled"},
    )
    marketplace_client = FakeDigisellerOrderClient(sales=[])

    stats = process_digiseller_unique_code(
        unique_code="UNIQUE-CODE-0005",
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["status"] == "partial"
    assert stats["errors"] == 1
    assert stats["supplier_orders_created"] == 0
    assert stats["orders_delivered"] == 0
    assert order.status == "needs_manual_review"
    assert order.supplier_order_id == "7473"
    assert order.error_message == "supplier_order_terminal:canceled"
    assert len(supplier_client.create_calls) == 0
    assert marketplace_client.delivered_unique_codes == []


def test_digiseller_order_with_unique_code_in_detail_delivers_in_sync(
    db_session: Session,
) -> None:
    _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[sale],
        order_payloads={
            "9001": {
                "retval": 0,
                "content": {
                    "item_id": 5850465,
                    "query_string": "?uniquecode=UNIQUE-CODE-0002",
                },
            }
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    order = db_session.execute(select(Order)).scalars().one()

    assert stats["orders_delivered"] == 1
    assert stats["supplier_orders_created"] == 1
    assert order.marketplace_order_id == "9001"
    assert order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["UNIQUE-CODE-0002"]


def test_digiseller_order_with_unique_code_in_chat_delivers_in_sync(
    db_session: Session,
) -> None:
    _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[sale],
        order_payloads={"9001": sale},
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "hello, unique code is E5E8B7E1369947A3",
                    "buyer": 1,
                    "seller": 0,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:01:00",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    order = db_session.execute(select(Order)).scalars().one()

    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["E5E8B7E1369947A3"]


def test_digiseller_code_posted_before_prompt_delivers_without_prompt(
    db_session: Session,
) -> None:
    _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    sale = {
        "invoice_id": "9001",
        "id_goods": "5850465",
        "date_pay": "2026-04-27 10:00:00",
        "amount": "5.00",
    }
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[sale],
        order_payloads={"9001": sale},
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "6E547DD6ADA24250",
                    "seller": 0,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:00:05",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    order = db_session.execute(select(Order)).scalars().one()

    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["6E547DD6ADA24250"]
    assert len(marketplace_client.messages) == 1
    assert all("please paste your unique code" not in message for _, message in marketplace_client.messages)


def test_order_sync_rechecks_local_pending_unique_code_orders_without_recent_sale(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="pending",
            error_message="awaiting_digiseller_unique_code",
        )
    )
    db_session.commit()

    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[],
        order_payloads={
            "9001": {
                "retval": 0,
                "content": {
                    "item_id": 5850465,
                },
            }
        },
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "unique code E5E8B7E1369947A3",
                    "buyer": 1,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:01:00",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )
    order = db_session.execute(select(Order)).scalars().one()

    assert stats["orders_detected"] == 1
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["E5E8B7E1369947A3"]


def test_order_sync_rechecks_pending_unique_code_chat_lookup_failures(
    db_session: Session,
) -> None:
    supplier = _add_linked_offer(db_session, marketplace="digiseller", offer_id="5850465")
    offer = db_session.execute(select(MarketplaceOffer)).scalars().one()
    db_session.add(
        Order(
            marketplace="digiseller",
            marketplace_order_id="9001",
            marketplace_offer_id=offer.id,
            supplier_product_id=supplier.id,
            status="pending",
            error_message="awaiting_digiseller_unique_code_chat_lookup_failed:timeout",
        )
    )
    db_session.commit()

    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeDigisellerOrderClient(
        sales=[],
        order_payloads={
            "9001": {
                "retval": 0,
                "content": {
                    "item_id": 5850465,
                },
            }
        },
        chat_messages={
            "9001": [
                {
                    "id": 1,
                    "message": "unique code E5E8B7E1369947A3",
                    "buyer": 1,
                    "deleted": 0,
                    "date_written": "2026-04-27 10:01:00",
                }
            ]
        },
    )

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    order = db_session.execute(select(Order)).scalars().one()
    assert stats["orders_detected"] == 1
    assert stats["orders_delivered"] == 1
    assert order.status == "delivered"
    assert len(supplier_client.create_calls) == 1
    assert marketplace_client.delivered_unique_codes == ["E5E8B7E1369947A3"]


def test_order_sync_poll_window_includes_configured_future_skew(
    db_session: Session,
) -> None:
    supplier_client = FakeSupplierOrderClient()
    marketplace_client = FakeMarketplaceOrderClient(sales=[])
    settings = Settings(
        _env_file=None,
        scheduler_enabled=False,
        order_sync_initial_lookback_hours=24,
        order_sync_future_window_minutes=360,
    )

    sync_orders(
        db=db_session,
        settings=settings,
        supplier_client=supplier_client,
        marketplace_clients={"digiseller": marketplace_client},
    )

    date_start, date_finish = marketplace_client.latest_sales_windows[0]
    assert date_start is not None
    assert date_finish is not None
    assert date_finish - datetime.now(date_finish.tzinfo) > timedelta(hours=5, minutes=55)


def test_parse_additive_quantity_sums_additive_notation() -> None:
    assert _parse_additive_quantity("32 + 3 Diamonds") == Decimal("35")
    assert _parse_additive_quantity("50 + 5 Diamonds") == Decimal("55")
    assert _parse_additive_quantity("35 Diamonds") == Decimal("35")
    assert _parse_additive_quantity("100 Gold") == Decimal("100")
    assert _parse_additive_quantity("Weekly Pass") is None
    assert _parse_additive_quantity("") is None
    assert _parse_additive_quantity(None) is None
    assert _parse_additive_quantity("10 + 2 + 3 Stars") == Decimal("15")


def test_ggsel_recharge_variant_matches_additive_denomination_name(
    db_session: Session,
) -> None:
    """Sale variant '35 Diamonds' matches mapping with name '32 + 3 Diamonds' via additive sum."""
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"5",'
        '"recharge_denomination_id":"101","recharge_param_keys":["Account"]}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:5:101",
        title="Mobile Legends 35 Diamonds",
        category="Recharge",
        denomination="32 + 3 Diamonds",
        region="Russia",
        cost_usd=Decimal("0.6834"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102270001",
        supplier_product_id=supplier.id,
        title="MLBB Diamonds Russia",
        price=Decimal("0.80"),
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
            marketplace_variant_id=None,
            marketplace_variant_name="32 + 3 Diamonds",
        )
    )
    db_session.commit()

    # GGSEL sale with variant_text "35 Diamonds" (sum of 32+3) but no matching variant_id
    sale = {
        "invoice_id": "9020",
        "id_goods": "102270001",
        "date_pay": "2026-05-01 10:00:00",
        "amount": "0.80",
        "denomination": "35 Diamonds",
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 8001, "status": "shipped", "supplier_order_id": "recharge:8001"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9020": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["product_id"] == "recharge:5:101"


def test_ggsel_recharge_variant_id_with_additive_text_fallback(
    db_session: Session,
) -> None:
    """When variant_id doesn't match any mapping, fall back to additive quantity from variant_text."""
    raw_payload_json = (
        '{"source_type":"recharge","recharge_product_id":"5",'
        '"recharge_denomination_id":"101","recharge_param_keys":["Account"]}'
    )
    supplier = SupplierProduct(
        supplier="gengine",
        supplier_product_id="recharge:5:101",
        title="Mobile Legends 35 Diamonds",
        category="Recharge",
        denomination="32 + 3 Diamonds",
        region="Russia",
        cost_usd=Decimal("0.6834"),
        supplier_stock="unlimited",
        is_available=True,
        raw_payload_json=raw_payload_json,
    )
    db_session.add(supplier)
    db_session.flush()

    offer = MarketplaceOffer(
        marketplace="ggsel",
        marketplace_offer_id="102270002",
        supplier_product_id=supplier.id,
        title="MLBB Diamonds Russia",
        price=Decimal("0.80"),
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
            marketplace_variant_id=None,
            marketplace_variant_name="32 + 3 Diamonds",
        )
    )
    db_session.commit()

    # GGSEL sale with a variant_id that has no matching mapping, but text "35 Diamonds" can match
    sale = {
        "invoice_id": "9021",
        "id_goods": "102270002",
        "date_pay": "2026-05-01 10:00:00",
        "amount": "0.80",
        "options": [
            {"name": "Diamonds", "user_data": "35 Diamonds", "user_data_id": 99999},
            {"name": "Account", "user_data": "player123", "user_data_id": None},
        ],
    }
    supplier_client = FakeSupplierOrderClient(
        create_response={"id": 8002, "status": "shipped", "supplier_order_id": "recharge:8002"}
    )
    marketplace_client = FakeMarketplaceOrderClient(sales=[sale], order_payloads={"9021": sale})

    stats = sync_orders(
        db=db_session,
        settings=_settings_for_tests(),
        supplier_client=supplier_client,
        marketplace_clients={"ggsel": marketplace_client},
    )

    assert stats["orders_delivered"] == 1
    assert supplier_client.create_calls[0]["product_id"] == "recharge:5:101"
