from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.clients import (
    APIClientConfigError,
    APIClientError,
    APIRequestError,
    DigisellerClient,
    GEngineClient,
    GGSELClient,
)


def _attach_mock_transport(
    client: GEngineClient | GGSELClient | DigisellerClient,
    handler: httpx.MockTransport | Any,
) -> None:
    client.http.close()
    client.http = httpx.Client(transport=handler, base_url=client.base_url, timeout=client.timeout)


def test_gengine_requires_api_key() -> None:
    client = GEngineClient(base_url="https://api.g-engine.net", api_key=None)
    with pytest.raises(APIClientConfigError):
        client.get_balance()
    client.close()


def test_gengine_create_order_uses_confirmed_endpoint_and_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["x_api_key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"ok": True})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(product_id="123", quantity=2)
    client.close()

    assert response == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v2.1/shop/orders")
    assert captured["x_api_key"] == "secret"
    assert captured["body"] == {"items": [{"id": 123, "quantity": 2}]}


def test_gengine_create_order_includes_buyer_inputs_for_direct_fulfillment() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"order_id": "ge-1"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        product_id="718",
        quantity=1,
        buyer_inputs={"steam_friend_code": "123456789"},
        external_order_id="ggsel:9001",
    )
    client.close()

    assert response == {"order_id": "ge-1", "supplier_order_id": "ge-1"}
    assert captured["body"] == {
        "items": [
            {
                "id": 718,
                "quantity": 1,
                "fields": {"steam_friend_code": "123456789"},
                "account": "123456789",
            }
        ],
        "external_id": "ggsel:9001",
    }


def test_gengine_create_shop_order_pays_pending_order() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}") if request.content else None
        captured.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/shop/orders"):
            return httpx.Response(
                status_code=200,
                json={"success": True, "data": {"id": 7473, "status": "pending"}},
            )
        if request.url.path.endswith("/shop/orders/7473/pay"):
            return httpx.Response(
                status_code=200,
                json={"success": True, "data": {"id": 7473, "status": "paid"}},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(product_id="123", quantity=1)
    client.close()

    assert response == {
        "success": True,
        "data": {"id": 7473, "status": "paid"},
        "supplier_order_id": "7473",
    }
    assert captured == [
        {"method": "POST", "path": "/v2.1/shop/orders", "body": {"items": [{"id": 123, "quantity": 1}]}},
        {"method": "POST", "path": "/v2.1/shop/orders/7473/pay", "body": None},
    ]


def test_gengine_shop_status_pays_existing_pending_order() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"method": request.method, "path": request.url.path})
        if request.url.path.endswith("/shop/orders/7473") and request.method == "GET":
            return httpx.Response(
                status_code=200,
                json={"success": True, "data": {"id": 7473, "status": "pending"}},
            )
        if request.url.path.endswith("/shop/orders/7473/pay"):
            return httpx.Response(
                status_code=200,
                json={"success": True, "data": {"id": 7473, "status": "paid"}},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.get_order_status("7473")
    client.close()

    assert response == {
        "success": True,
        "data": {"id": 7473, "status": "paid"},
        "supplier_order_id": "7473",
    }
    assert captured == [
        {"method": "GET", "path": "/v2.1/shop/orders/7473"},
        {"method": "POST", "path": "/v2.1/shop/orders/7473/pay"},
    ]


def test_gengine_shop_status_does_not_pay_cancelled_order() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"method": request.method, "path": request.url.path})
        return httpx.Response(
            status_code=200,
            json={"success": True, "data": {"id": 7473, "status": "canceled"}},
        )

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.get_order_status("7473")
    client.close()

    assert response == {
        "success": True,
        "data": {"id": 7473, "status": "canceled"},
        "supplier_order_id": "7473",
    }
    assert captured == [{"method": "GET", "path": "/v2.1/shop/orders/7473"}]


def test_gengine_create_gift_order_uses_gifts_endpoint_without_pay_step() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}") if request.content else None
        captured.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/gifts/orders"):
            return httpx.Response(status_code=200, json={"id": 9001, "status": "accepted"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "gift:4451080:777:TR",
        buyer_inputs={"steam_invite_url": "https://steamcommunity.com/invite/abc"},
        external_order_id="ggsel:9001",
        product_payload={"source_type": "gift"},
    )
    client.close()

    assert response == {"id": 9001, "status": "accepted", "supplier_order_id": "gift:9001"}
    assert captured == [
        {
            "method": "POST",
            "path": "/v2.1/gifts/orders",
            "body": {
                "invite_url": "https://steamcommunity.com/invite/abc",
                "package_id": 777,
                "region": "TR",
            },
        }
    ]


def test_gengine_create_gift_order_prefers_payload_region_for_cis_alias() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}") if request.content else None
        captured.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/gifts/orders"):
            return httpx.Response(status_code=200, json={"id": 9002, "status": "accepted"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "gift:1238840:432909:RU-CIS",
        buyer_inputs={"quick_invite_steam_url": "https://steamcommunity.com/invite/abc"},
        external_order_id="ggsel:9002",
        product_payload={
            "source_type": "gift",
            "gift_package_id": "432909",
            "gift_region": "RU+CIS",
        },
    )
    client.close()

    assert response == {"id": 9002, "status": "accepted", "supplier_order_id": "gift:9002"}
    assert captured == [
        {
            "method": "POST",
            "path": "/v2.1/gifts/orders",
            "body": {
                "invite_url": "https://steamcommunity.com/invite/abc",
                "package_id": 432909,
                "region": "RU+CIS",
            },
        }
    ]


def test_gengine_create_gift_order_uses_ge_api_region_for_cis_zone() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}") if request.content else None
        captured.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/gifts/orders"):
            return httpx.Response(status_code=200, json={"id": 9003, "status": "accepted"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "gift:1238840:432909:ge",
        buyer_inputs={"quick_invite_steam_url": "https://steamcommunity.com/invite/abc"},
        external_order_id="ggsel:9003",
        product_payload={
            "source_type": "gift",
            "gift_package_id": "432909",
            "gift_region": "ge",
            "gift_zone": "CIS",
        },
    )
    client.close()

    assert response == {"id": 9003, "status": "accepted", "supplier_order_id": "gift:9003"}
    assert captured == [
        {
            "method": "POST",
            "path": "/v2.1/gifts/orders",
            "body": {
                "invite_url": "https://steamcommunity.com/invite/abc",
                "package_id": 432909,
                "region": "ge",
            },
        }
    ]


def test_gengine_gift_status_uses_gifts_endpoint() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"method": request.method, "path": request.url.path})
        if request.url.path.endswith("/gifts/orders/9001"):
            return httpx.Response(status_code=200, json={"id": 9001, "status": "delivered"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.get_order_status("gift:9001")
    client.close()

    assert response == {"id": 9001, "status": "delivered", "supplier_order_id": "gift:9001"}
    assert captured == [{"method": "GET", "path": "/v2.1/gifts/orders/9001"}]


def test_gengine_gift_order_requires_invite_url() -> None:
    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")

    with pytest.raises(APIClientError, match="invite_url"):
        client.create_order(
            "gift:4451080:777:TR",
            buyer_inputs={},
            product_payload={"source_type": "gift"},
        )

    client.close()


def test_gengine_create_recharge_order_posts_params_and_pays_when_verified() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}") if request.content else None
        captured.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/recharge/orders/1"):
            return httpx.Response(status_code=200, json={"id": 7001, "status": "verified"})
        if request.url.path.endswith("/recharge/orders/7001/pay"):
            return httpx.Response(status_code=200, json={"id": 7001, "status": "paid"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "recharge:1",
        buyer_inputs={"steam_friend_code": "123456789"},
        external_order_id="ggsel:9001",
        product_payload={
            "source_type": "recharge",
            "recharge_product_id": "1",
            "recharge_param_keys": ["Account", "Quantity"],
            "recharge_default_params": {"Quantity": "1000"},
        },
    )
    client.close()

    assert response == {"id": 7001, "status": "paid", "supplier_order_id": "recharge:7001"}
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "/v2.1/recharge/orders/1"
    assert captured[0]["body"]["params"] == [
        {"param_key": "Account", "param_value": "123456789"},
        {"param_key": "Quantity", "param_value": "1000"},
    ]
    assert len(str(captured[0]["body"]["uuid"])) == 32
    assert captured[1] == {
        "method": "POST",
        "path": "/v2.1/recharge/orders/7001/pay",
        "body": None,
    }


def test_gengine_recharge_account_fallback_skips_currency_like_inputs() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8") or "{}")
        return httpx.Response(status_code=200, json={"id": 7004, "status": "processing"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "recharge:1",
        buyer_inputs={"currency": "RUB", "steam_friend_code": "123456789"},
        product_payload={
            "source_type": "recharge",
            "recharge_product_id": "1",
            "recharge_param_keys": ["Account", "Quantity"],
            "recharge_default_params": {"Quantity": "1000"},
        },
    )
    client.close()

    assert response == {"id": 7004, "status": "processing", "supplier_order_id": "recharge:7004"}
    assert captured["body"]["params"] == [
        {"param_key": "Account", "param_value": "123456789"},
        {"param_key": "Quantity", "param_value": "1000"},
    ]


def test_gengine_create_recharge_order_uses_marketplace_quantity_for_direct_topup() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"id": 7003, "status": "processing"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "recharge:1",
        quantity=250,
        buyer_inputs={"steam_friend_code": "123456789"},
        product_payload={
            "source_type": "recharge",
            "recharge_product_id": "1",
            "recharge_param_keys": ["Account", "Quantity"],
            "recharge_quantity_source": "marketplace_quantity",
            "recharge_quantity_multiplier": "1",
        },
    )
    client.close()

    assert response == {"id": 7003, "status": "processing", "supplier_order_id": "recharge:7003"}
    assert captured["path"] == "/v2.1/recharge/orders/1"
    assert captured["body"]["params"] == [
        {"param_key": "Account", "param_value": "123456789"},
        {"param_key": "Quantity", "param_value": "250"},
    ]


def test_gengine_recharge_buyer_quantity_overrides_marketplace_count_default() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"id": 7005, "status": "processing"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "recharge:1",
        quantity=1,
        buyer_inputs={"account": "steam_friend_123", "quantity": "5"},
        product_payload={
            "source_type": "recharge",
            "recharge_product_id": "1",
            "recharge_param_keys": ["Account", "Quantity"],
            "recharge_quantity_source": "marketplace_quantity",
            "recharge_quantity_multiplier": "1",
        },
    )
    client.close()

    assert response == {"id": 7005, "status": "processing", "supplier_order_id": "recharge:7005"}
    assert captured["body"]["params"] == [
        {"param_key": "Account", "param_value": "steam_friend_123"},
        {"param_key": "Quantity", "param_value": "5"},
    ]


def test_gengine_create_fixed_recharge_order_includes_denomination_id() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"id": 7002, "status": "processing"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.create_order(
        "recharge:2:10",
        buyer_inputs={"account": "player-1"},
        product_payload={
            "source_type": "recharge",
            "recharge_product_id": "2",
            "recharge_denomination_id": "10",
            "recharge_param_keys": ["Account"],
        },
    )
    client.close()

    assert response == {"id": 7002, "status": "processing", "supplier_order_id": "recharge:7002"}
    assert captured["path"] == "/v2.1/recharge/orders/2"
    assert captured["body"]["denomination_id"] == 10
    assert captured["body"]["params"] == [{"param_key": "Account", "param_value": "player-1"}]


def test_gengine_recharge_status_pays_when_order_becomes_verified() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        if request.url.path.endswith("/recharge/orders/7001"):
            return httpx.Response(status_code=200, json={"id": 7001, "status": "verified"})
        if request.url.path.endswith("/recharge/orders/7001/pay"):
            return httpx.Response(status_code=200, json={"id": 7001, "status": "paid"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    response = client.get_order_status("recharge:7001")
    client.close()

    assert response == {"id": 7001, "status": "paid", "supplier_order_id": "recharge:7001"}
    assert captured_paths == [
        "/v2.1/recharge/orders/7001",
        "/v2.1/recharge/orders/7001/pay",
    ]


def test_gengine_recharge_order_requires_quantity_for_unfixed_service() -> None:
    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")

    with pytest.raises(APIClientError, match="Quantity"):
        client.create_order(
            "recharge:1",
            buyer_inputs={"steam_friend_code": "123456789"},
            product_payload={
                "source_type": "recharge",
                "recharge_product_id": "1",
                "recharge_param_keys": ["Account", "Quantity"],
            },
        )

    client.close()


def test_gengine_does_not_duplicate_v21_prefix_if_present_in_base_url() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(status_code=200, json={"data": {}})

    client = GEngineClient(base_url="https://api.g-engine.net/v2.1", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))
    client.get_balance()
    client.close()

    assert captured["url"].endswith("/v2.1/users/balance")


def test_gengine_list_products_expands_denominations() -> None:
    call_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        call_paths.append(path)
        if path.endswith("/shop/products"):
            return httpx.Response(
                status_code=200,
                json={
                    "data": {
                        "items": [
                            {
                                "id": "100",
                                "name": "Steam Gift Card",
                                "category": "Gift Cards",
                            }
                        ]
                    }
                },
            )
        if path.endswith("/shop/denominations/100"):
            return httpx.Response(
                status_code=200,
                json={
                    "data": {
                        "denominations": [
                            {
                                "id": "1001",
                                "price": "5.50",
                                "denomination": "10",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_products()
    client.close()

    assert len(products) == 1
    assert products[0]["id"] == "1001"
    assert products[0]["product_id"] == "100"
    assert products[0]["price"] == "5.50"
    assert products[0]["name"] == "Steam Gift Card"
    assert any(path.endswith("/shop/denominations/100") for path in call_paths)


def test_gengine_list_product_denominations_fetches_direct_product_id() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        if request.url.path.endswith("/shop/denominations/5"):
            return httpx.Response(
                status_code=200,
                json={
                    "data": {
                        "denominations": [
                            {
                                "id": "5001",
                                "price": "5.50",
                                "denomination": "250 TRY",
                                "stock": "300+",
                            },
                            {
                                "id": "5002",
                                "price": "11.00",
                                "denomination": "500 TRY",
                                "stock": "300+",
                            },
                        ]
                    }
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_product_denominations(
        "5",
        fallback_title="Playstation Store Wallet Turkey",
        source_slug="playstation-store-wallet-turkey",
    )
    client.close()

    assert captured_paths == ["/v2.1/shop/denominations/5"]
    assert products == [
        {
            "product_id": "5",
            "base_product_id": "5",
            "source_slug": "playstation-store-wallet-turkey",
            "title": "Playstation Store Wallet Turkey",
            "name": "Playstation Store Wallet Turkey",
            "id": "5001",
            "price": "5.50",
            "denomination": "250 TRY",
            "stock": "300+",
        },
        {
            "product_id": "5",
            "base_product_id": "5",
            "source_slug": "playstation-store-wallet-turkey",
            "title": "Playstation Store Wallet Turkey",
            "name": "Playstation Store Wallet Turkey",
            "id": "5002",
            "price": "11.00",
            "denomination": "500 TRY",
            "stock": "300+",
        },
    ]


def test_gengine_list_gift_app_products_expands_package_regions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gifts/apps/4451080"):
            return httpx.Response(
                status_code=200,
                json={
                    "id": 4451080,
                    "name": "Vituber Melody",
                    "type": "game",
                    "packages": [
                        {
                            "id": 777,
                            "name": "Vituber Melody",
                            "prices": [
                                {
                                    "region": "TR",
                                    "currency": "USD",
                                    "price": 5.5,
                                    "zone": "Turkey",
                                }
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_gift_app_products(
        "4451080",
        fallback_title="Vituber Melody",
        source_slug="vituber-melody",
    )
    client.close()

    assert products == [
        {
            "id": "gift:4451080:777:TR",
            "name": "Vituber Melody",
            "type": "game",
            "packages": [
                {
                    "id": 777,
                    "name": "Vituber Melody",
                    "prices": [
                        {
                            "region": "TR",
                            "currency": "USD",
                            "price": 5.5,
                            "zone": "Turkey",
                        }
                    ],
                }
            ],
            "prices": [
                {
                    "region": "TR",
                    "currency": "USD",
                    "price": 5.5,
                    "zone": "Turkey",
                }
            ],
            "region": "TR",
            "currency": "USD",
            "price": 5.5,
            "zone": "Turkey",
            "source_type": "gift",
            "gift_app_id": "4451080",
            "gift_id": "4451080",
            "gift_package_id": "777",
            "gift_region": "TR",
            "package_id": "777",
            "title": "Vituber Melody",
            "category": "Games, Steam Gifts",
            "denomination": "TR",
            "cost_usd": "5.5",
            "stock": "unlimited",
            "is_available": True,
            "source_slug": "vituber-melody",
            "base_product_id": "4451080",
            "gift_package_name": "Vituber Melody",
            "gift_currency": "USD",
            "gift_zone": "Turkey",
        }
    ]


def test_gengine_list_products_includes_fixed_recharge_denominations() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        if request.url.path.endswith("/shop/products"):
            return httpx.Response(status_code=200, json={"data": {"items": []}})
        if request.url.path.endswith("/recharge/services"):
            return httpx.Response(
                status_code=200,
                json={
                    "total": 1,
                    "items": [
                        {
                            "id": 2,
                            "name": "Genshin Impact",
                            "description": "Genesis Crystals",
                            "type": "fixed",
                            "params": [{"param_key": "Account"}],
                            "denominations": [
                                {
                                    "id": 10,
                                    "service_id": 2,
                                    "name": "60 Genesis Crystals",
                                    "value": "60",
                                    "currency": "USD",
                                    "price": 0.99,
                                }
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_products()
    client.close()

    assert any(path.endswith("/recharge/services") for path in captured_paths)
    assert len(products) == 1
    assert products[0]["id"] == "recharge:2:10"
    assert products[0]["source_type"] == "recharge"
    assert products[0]["recharge_product_id"] == "2"
    assert products[0]["recharge_denomination_id"] == "10"
    assert products[0]["title"] == "Genshin Impact"
    assert products[0]["denomination"] == "60 Genesis Crystals"
    assert products[0]["cost_usd"] == "0.99"
    assert products[0]["stock"] == "unlimited"
    assert products[0]["is_available"] is True


def test_gengine_list_recharge_service_products_imports_unpriced_placeholder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recharge/services"):
            return httpx.Response(
                status_code=200,
                json={
                    "total": 1,
                    "items": [
                        {
                            "id": 1,
                            "name": "Steam Wallet RUB",
                            "description": "Steam top up",
                            "type": "unfixed",
                            "params": [
                                {"param_key": "Account", "param_type": "String"},
                                {"param_key": "Quantity", "param_type": "Float"},
                            ],
                            "denominations": None,
                            "unfixed_details": {"currency": "RUB", "in_game_currency": None},
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_recharge_service_products(
        "1",
        fallback_title="Steam",
        source_slug="steam",
        include_unpriced=True,
    )
    client.close()

    assert products == [
        {
            "id": "recharge:1",
            "name": "Steam Wallet RUB",
            "description": "Steam top up",
            "type": "unfixed",
            "params": [
                {"param_key": "Account", "param_type": "String"},
                {"param_key": "Quantity", "param_type": "Float"},
            ],
            "denominations": None,
            "unfixed_details": {"currency": "RUB", "in_game_currency": None},
            "source_type": "recharge",
            "recharge_product_id": "1",
            "service_id": "1",
            "service_name": "Steam Wallet RUB",
            "service_type": "unfixed",
            "source_slug": "steam",
            "title": "Steam Wallet RUB",
            "category": "Recharge",
            "recharge_param_keys": ["Account", "Quantity"],
            "denomination": "Variable RUB",
            "cost_usd": "0",
            "stock": "0",
            "is_available": False,
            "requires_manual_cost": True,
        }
    ]


def test_gengine_list_recharge_service_products_uses_steam_link_fallback_when_catalogue_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recharge/services"):
            return httpx.Response(status_code=200, json={"total": 0, "items": []})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_recharge_service_products(
        "1",
        fallback_title="Steam",
        source_slug="steam",
        include_unpriced=True,
    )
    client.close()

    assert len(products) == 1
    assert products[0]["id"] == "recharge:1"
    assert products[0]["recharge_product_id"] == "1"
    assert products[0]["recharge_param_keys"] == ["Account", "Quantity"]
    assert products[0]["requires_manual_cost"] is True
    assert products[0]["import_note"] == "fallback_from_recharge_link"


def test_gengine_list_recharge_services_continues_when_server_caps_page_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if request.url.path.endswith("/recharge/services"):
            if offset == 0:
                return httpx.Response(
                    status_code=200,
                    json={"items": [{"id": 9, "name": "Other"}]},
                )
            if offset == 1:
                return httpx.Response(
                    status_code=200,
                    json={"items": [{"id": 1, "name": "Steam Wallet RUB"}]},
                )
            return httpx.Response(status_code=200, json={"items": []})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    services = client.list_recharge_services()
    client.close()

    assert [item["id"] for item in services] == [9, 1]


def test_gengine_list_products_falls_back_to_base_product_when_denominations_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/shop/products"):
            return httpx.Response(
                status_code=200,
                json={"data": {"items": [{"id": "100", "title": "Base Product"}]}},
            )
        if path.endswith("/shop/denominations/100"):
            return httpx.Response(status_code=500, json={"error": "temporary"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GEngineClient(base_url="https://api.g-engine.net", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    products = client.list_products()
    client.close()

    assert len(products) == 1
    assert products[0]["id"] == "100"
    assert products[0]["title"] == "Base Product"


def test_ggsel_unresolved_methods_remain_explicitly_not_implemented() -> None:
    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    with pytest.raises(NotImplementedError):
        client.create_offer({"title": "x"})
    client.close()


def test_digiseller_set_offer_price_supports_variants_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            captured["query"] = dict(request.url.params)
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="10",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert captured["query"]["token"] == "secret"
    assert captured["json"] == [
        {
            "product_id": 10,
            "price": 7.75,
            "variants": [{"variant_id": 5001, "rate": 7.35, "type": 2}],
        }
    ]


def test_digiseller_set_offer_price_accepts_plain_text_task_id_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            return httpx.Response(
                status_code=200,
                text="9a968c22-9bc2-439f-bbaf-0f5a368f59a5",
                headers={"Content-Type": "text/plain"},
            )
        if request.url.path.endswith("/api/product/edit/UpdateProductsTaskStatus"):
            return httpx.Response(
                status_code=200,
                json={
                    "TaskId": "9a968c22-9bc2-439f-bbaf-0f5a368f59a5",
                    "Status": 3,
                    "SuccessCount": 1,
                    "ErrorCount": 0,
                    "TotalCount": 1,
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="10",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result["status"] == "ok"
    assert result["task_id"] == "9a968c22-9bc2-439f-bbaf-0f5a368f59a5"


def test_digiseller_set_offer_price_raises_when_async_task_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            return httpx.Response(
                status_code=200,
                json={"taskId": "c41ab768-2e96-4682-9d5b-c460c8c6c955"},
            )
        if request.url.path.endswith("/api/product/edit/UpdateProductsTaskStatus"):
            return httpx.Response(
                status_code=200,
                json={
                    "TaskId": "c41ab768-2e96-4682-9d5b-c460c8c6c955",
                    "Status": 2,
                    "SuccessCount": 0,
                    "ErrorCount": 1,
                    "TotalCount": 1,
                    "ErrorsDescriptions": [
                        {
                            "Key": "5850465",
                            "Value": "Product not found or no permission to edit",
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError, match="no permission to edit"):
        client.set_offer_price(
            offer_id="5850465",
            price=7.75,
            currency="USD",
            variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
        )
    client.close()


def test_digiseller_set_offer_price_raises_when_task_done_has_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            return httpx.Response(
                status_code=200,
                json={"taskId": "task-done-with-errors"},
            )
        if request.url.path.endswith("/api/product/edit/UpdateProductsTaskStatus"):
            return httpx.Response(
                status_code=200,
                json={
                    "TaskId": "task-done-with-errors",
                    "Status": 3,
                    "SuccessCount": 0,
                    "ErrorCount": 1,
                    "TotalCount": 1,
                    "ErrorsDescriptions": [
                        {
                            "Key": "19472020",
                            "Value": "Variant not found",
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError, match="completed with errors"):
        client.set_offer_price(
            offer_id="5850465",
            price=7.75,
            currency="USD",
            variants=[{"variant_id": 19472020, "rate": 7.35, "type": 2}],
        )
    client.close()


def test_digiseller_set_offer_price_returns_pending_when_task_timeout_in_queue() -> None:
    calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            return httpx.Response(
                status_code=200,
                json={"taskId": "pending-task-id"},
            )
        if request.url.path.endswith("/api/product/edit/UpdateProductsTaskStatus"):
            calls["status"] += 1
            return httpx.Response(
                status_code=200,
                json={
                    "TaskId": "pending-task-id",
                    "Status": 0,
                    "SuccessCount": 0,
                    "ErrorCount": 0,
                    "TotalCount": 1,
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    client._task_poll_timeout_seconds = 0.01  # type: ignore[attr-defined]
    client._task_poll_interval_seconds = 0.01  # type: ignore[attr-defined]
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="5850465",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 19472020, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result.get("status") == "pending"
    assert result.get("task_id") == "pending-task-id"
    assert calls["status"] >= 1


def test_digiseller_set_offer_price_retries_with_header_auth_after_401() -> None:
    captured: dict[str, Any] = {"attempts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/prices"):
            captured["attempts"].append(
                {
                    "query": dict(request.url.params),
                    "auth_header": request.headers.get("Authorization"),
                    "token_header": request.headers.get("token"),
                }
            )
            if len(captured["attempts"]) == 1:
                return httpx.Response(status_code=401, json={"error": "unauthorized"})
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="10",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert len(captured["attempts"]) == 2

    first = captured["attempts"][0]
    assert first["query"]["token"] == "secret"
    assert first["auth_header"] is None

    second = captured["attempts"][1]
    assert second["query"] == {}
    assert second["token_header"] == "secret"
    assert second["auth_header"] == "Bearer secret"


def test_digiseller_set_offer_price_uses_apilogin_token_when_seller_id_is_configured() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/apilogin"):
            payload = json.loads(request.content.decode("utf-8"))
            captured["apilogin_payload"] = payload
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "token": "token-from-apilogin",
                    "seller_id": 123456,
                    "valid_thru": "2099-01-01T00:00:00Z",
                },
            )
        if request.url.path.endswith("/api/product/edit/prices"):
            captured["price_query"] = dict(request.url.params)
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(
        base_url="https://api.digiseller.com",
        api_key="seller-api-key",
        seller_id="123456",
    )
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="10",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert captured["apilogin_payload"]["seller_id"] == 123456
    assert isinstance(captured["apilogin_payload"]["timestamp"], int)
    assert isinstance(captured["apilogin_payload"]["sign"], str)
    assert captured["price_query"]["token"] == "token-from-apilogin"
    assert captured["price_json"] == [
        {
            "product_id": 10,
            "price": 7.75,
            "variants": [{"variant_id": 5001, "rate": 7.35, "type": 2}],
        }
    ]


def test_digiseller_set_offer_price_refreshes_apilogin_token_after_401() -> None:
    state = {"apilogin_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/apilogin"):
            state["apilogin_calls"] += 1
            token = "token-a" if state["apilogin_calls"] == 1 else "token-b"
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "token": token,
                    "seller_id": 123456,
                    "valid_thru": "2099-01-01T00:00:00Z",
                },
            )
        if request.url.path.endswith("/api/product/edit/prices"):
            token = request.url.params.get("token")
            if token == "token-a":
                return httpx.Response(status_code=401, json={"error": "unauthorized"})
            if token == "token-b":
                return httpx.Response(status_code=200, json={"status": "ok"})
            return httpx.Response(status_code=400, json={"error": "unexpected token"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(
        base_url="https://api.digiseller.com",
        api_key="seller-api-key",
        seller_id="123456",
    )
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="10",
        price=7.75,
        currency="USD",
        variants=[{"variant_id": 5001, "rate": 7.35, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert state["apilogin_calls"] == 2


def test_digiseller_set_offer_enabled_uses_bulk_status_endpoint() -> None:
    calls: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/product/edit/V2/status"):
            calls["status_payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "retdesc": None, "errors": None, "content": "task-123"},
            )
        if request.url.path.endswith("/api/product/edit/UpdateProductsTaskStatus"):
            calls["status_query"] = dict(request.url.params)
            return httpx.Response(
                status_code=200,
                json={"TaskId": "task-123", "Status": 3, "ErrorCount": 0},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_enabled("5850465", False)
    client.close()

    assert result.get("status") == "ok"
    assert result.get("task_id") == "task-123"
    assert calls["status_payload"] == {"new_status": "disabled", "products": [5850465]}
    assert calls["status_query"]["taskId"] == "task-123"


def test_digiseller_set_offer_variant_visible_edits_target_variant() -> None:
    calls: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/products/options/list/5850465"):
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "content": [{"id": 777}]},
            )
        if request.url.path.endswith("/api/products/options/777"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "content": {
                        "id": 777,
                        "variants": [
                            {
                                "variant_id": 19472011,
                                "name": [{"locale": "en-US", "value": "250 TRY"}],
                                "type": "priceplus",
                                "rate": 0,
                                "is_default": True,
                                "visible": True,
                                "order": 1,
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/api/products/options/777/variants/19472011"):
            calls["variant_payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "content": {"status": "Success"}},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_variant_visible("5850465", 19472011, False)
    client.close()

    assert result["status"] == "ok"
    assert result["variant_id"] == 19472011
    assert result["visible"] is False
    assert calls["variant_payload"]["visible"] is False


def test_digiseller_unique_code_delivery_uses_confirmed_endpoints() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/purchases/unique-code/ABC1234567890000"):
            captured["verify_query"] = dict(request.url.params)
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "inv": 9001, "id_goods": 5850465},
            )
        if request.url.path.endswith("/api/purchases/unique-code/ABC1234567890000/deliver"):
            captured["deliver_query"] = dict(request.url.params)
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "inv": 9001, "id_goods": 5850465},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    verify_result = client.verify_unique_code("ABC1234567890000")
    deliver_result = client.mark_unique_code_delivered("ABC1234567890000")
    client.close()

    assert verify_result["inv"] == 9001
    assert deliver_result["retval"] == 0
    assert captured["verify_query"]["token"] == "secret"
    assert captured["deliver_query"]["token"] == "secret"


def test_digiseller_unique_code_error_masks_code_in_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/purchases/unique-code/ABC1234567890000"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError) as exc_info:
        client.verify_unique_code("ABC1234567890000")
    client.close()

    error_text = str(exc_info.value)
    assert "ABC1234567890000" not in error_text
    assert "AB************00" in error_text


def test_digiseller_send_buyer_message_uses_order_as_dialog_id() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/debates/v2"):
            captured["query"] = dict(request.url.params)
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, content=b"")
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.send_buyer_message(order_id="9001", buyer_id="", message="hello")
    client.close()

    assert result == {"status": "ok"}
    assert captured["query"] == {"token": "secret", "id_i": "9001"}
    assert captured["json"] == {"message": "hello", "files": []}


def test_digiseller_get_buyer_messages_reads_order_chat() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/debates/v2"):
            captured["query"] = dict(request.url.params)
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "id": 56627081,
                        "message": "unique code is E5E8B7E1369947A3",
                        "buyer": 1,
                        "seller": 0,
                    }
                ],
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    messages = client.get_buyer_messages("9001")
    client.close()

    assert captured["query"] == {
        "token": "secret",
        "id_i": "9001",
        "hidden": "0",
        "count": "50",
    }
    assert messages[0]["message"] == "unique code is E5E8B7E1369947A3"


def test_digiseller_get_buyer_messages_reads_wrapped_order_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/debates/v2"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "content": {
                        "messages": [
                            {
                                "id": 56627081,
                                "message": "code E5E8B7E1369947A3",
                                "buyer": 1,
                            }
                        ]
                    },
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    messages = client.get_buyer_messages("9001")
    client.close()

    assert messages == [
        {
            "id": 56627081,
            "message": "code E5E8B7E1369947A3",
            "buyer": 1,
        }
    ]


def test_digiseller_get_latest_sales_reads_official_rows_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        if request.url.path.endswith("/api/seller-sells/v2"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "total_rows": 1,
                    "pages": 1,
                    "page": 1,
                    "rows": [
                        {
                            "invoice_id": 123456789,
                            "product_id": 5856640,
                            "product_name": "STEAM USD",
                            "date_pay": "2026-04-29 02:05:00",
                            "amount_in": 7.57,
                            "amount_currency": "WMZ",
                            "returned": 0,
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    sales = client.get_latest_sales()
    client.close()

    assert captured["path"].endswith("/api/seller-sells/v2")
    assert captured["query"]["token"] == "secret"
    assert "date_start" in captured["body"]
    assert "date_finish" in captured["body"]
    assert captured["body"]["returned"] == 1
    assert captured["body"]["rows"] == 5000
    assert sales == [
        {
            "invoice_id": 123456789,
            "product_id": 5856640,
            "product_name": "STEAM USD",
            "date_pay": "2026-04-29 02:05:00",
            "amount_in": 7.57,
            "amount_currency": "WMZ",
            "returned": 0,
        }
    ]


def test_digiseller_variant_update_error_includes_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/products/options/list/5850465"):
            return httpx.Response(
                status_code=200,
                json={"retval": 0, "content": [{"id": 777}]},
            )
        if request.url.path.endswith("/api/products/options/777"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "content": {
                        "id": 777,
                        "variants": [
                            {
                                "variant_id": 19472011,
                                "name": [{"locale": "en-US", "value": "250 TRY"}],
                                "type": "priceplus",
                                "rate": 0,
                                "is_default": True,
                                "visible": True,
                                "order": 1,
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/api/products/options/777/variants/19472011"):
            return httpx.Response(
                status_code=400,
                json={"message": "default variant cannot be hidden"},
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError, match="default variant cannot be hidden"):
        client.set_offer_variant_visible("5850465", 19472011, False)
    client.close()


def test_digiseller_listing_create_and_base_update_are_disabled() -> None:
    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    with pytest.raises(NotImplementedError):
        client.create_offer({"title": "x"})
    with pytest.raises(NotImplementedError):
        client.update_offer(offer_id="10", payload={"title": "x"})
    client.close()


def test_ggsel_set_offer_variant_visible_edits_target_variant() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/products/options/list/5850465"):
            return httpx.Response(status_code=200, json={"retval": 0, "content": [{"id": 777}]})
        if request.url.path.endswith("/api_sellers/api/products/options/777"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "content": {
                        "id": 777,
                        "variants": [
                            {
                                "variant_id": 19472020,
                                "name": [{"locale": "en-US", "value": "500 TRY"}],
                                "type": "priceplus",
                                "rate": 5.0,
                                "is_default": False,
                                "visible": True,
                                "order": 2,
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/api_sellers/api/products/options/777/variants/19472020"):
            captured["query"] = dict(request.url.params)
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"retval": 0})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_variant_visible("5850465", 19472020, False)
    client.close()

    assert result["status"] == "ok"
    assert result["variant_id"] == 19472020
    assert captured["query"]["token"] == "secret"
    assert captured["json"]["visible"] is False


def test_ggsel_set_offer_variant_visible_uses_product_data_options() -> None:
    captured: dict[str, Any] = {"paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["paths"].append(request.url.path)
        if request.url.path.endswith("/api_sellers/api/products/102260467/data"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "product": {
                        "id": 102260467,
                        "options": [
                            {
                                "name": 777,
                                "label": "Country",
                                "variants": [
                                    {
                                        "value": 19224615,
                                        "text": "CIS",
                                        "default": 0,
                                        "modify": "1",
                                        "modify_type": "priceplus",
                                        "modify_value": "76.23",
                                        "num_in_stock": 0,
                                        "visible": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/api_sellers/api/products/options/777/variants/19224615"):
            captured["query"] = dict(request.url.params)
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"retval": 0})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_variant_visible("102260467", 19224615, False)
    client.close()

    assert result["status"] == "ok"
    assert result["option_id"] == 777
    assert result["variant_id"] == 19224615
    assert captured["query"]["token"] == "secret"
    assert captured["json"]["text"] == "CIS"
    assert captured["json"]["modify_value"] == "76.23"
    assert captured["json"]["visible"] is False
    assert "/api_sellers/api/products/options/list/102260467" not in captured["paths"]


def test_ggsel_send_buyer_message_uses_prefixed_debates_endpoint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/debates/v2"):
            captured["query"] = dict(request.url.params)
            captured["json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, content=b"")
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.send_buyer_message(order_id="9001", buyer_id="", message="hello")
    client.close()

    assert result == {"status": "ok"}
    assert captured["query"] == {"token": "secret", "id_i": "9001"}
    assert captured["json"] == {"message": "hello", "files": []}


def test_ggsel_send_buyer_message_refreshes_apilogin_token_after_401() -> None:
    state = {"apilogin_calls": 0, "message_tokens": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/apilogin"):
            state["apilogin_calls"] += 1
            token = "ggsel-message-token-a" if state["apilogin_calls"] == 1 else "ggsel-message-token-b"
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "token": token,
                    "seller_id": 123456,
                    "valid_thru": "2099-01-01T00:00:00Z",
                },
            )
        if request.url.path.endswith("/api_sellers/api/debates/v2"):
            token = str(request.url.params.get("token"))
            state["message_tokens"].append(token)
            if token == "ggsel-message-token-a":
                return httpx.Response(status_code=401, text="Unauthorized")
            if token == "ggsel-message-token-b":
                return httpx.Response(status_code=200, content=b"")
            return httpx.Response(status_code=400, json={"error": "unexpected token"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(
        base_url="https://seller.ggsel.com",
        api_key="seller-api-key",
        seller_id="123456",
    )
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.send_buyer_message(order_id="9001", buyer_id="", message="hello")
    client.close()

    assert result == {"status": "ok"}
    assert state["apilogin_calls"] == 2
    assert state["message_tokens"] == ["ggsel-message-token-a", "ggsel-message-token-b"]


def test_ggsel_get_latest_sales_reads_sales_payload_and_flattens_product() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        if request.url.path.endswith("/api_sellers/api/seller-last-sales"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "sales": [
                        {
                            "date": "2026-04-29 02:05:00",
                            "invoice_id": 123456789,
                            "product": {
                                "id": 5856640,
                                "name": "STEAM USD",
                                "price_usd": 7.57,
                            },
                        }
                    ],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    sales = client.get_latest_sales()
    client.close()

    assert captured["path"].endswith("/api_sellers/api/seller-last-sales")
    assert captured["query"]["token"] == "secret"
    assert captured["query"]["group"] == "false"
    assert captured["query"]["top"] == "1000"
    assert sales == [
        {
            "date": "2026-04-29 02:05:00",
            "invoice_id": 123456789,
            "product": {"id": 5856640, "name": "STEAM USD", "price_usd": 7.57},
            "product_id": 5856640,
            "product_name": "STEAM USD",
            "amount_usd": 7.57,
        }
    ]


def test_ggsel_get_offer_by_id_filters_from_seller_goods() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/seller-goods"):
            return httpx.Response(
                status_code=200,
                json={
                    "data": [
                        {"id": "101", "name": "PSN 100 TRY"},
                        {"id": "5850465", "name": "PSN 250 TRY", "price": "7.00"},
                    ]
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("5850465")
    client.close()

    assert offer is not None
    assert offer["id"] == "5850465"


def test_ggsel_get_offer_by_id_matches_id_goods_from_seller_goods() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/products/102249445/data"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        if request.url.path.endswith("/api_sellers/api/product/102249445/data"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        if request.url.path.endswith("/api_sellers/api/products/list"):
            return httpx.Response(
                status_code=200,
                json={"data": [{"id_goods": "111", "name_goods": "Other product"}]},
            )
        if request.url.path.endswith("/api_sellers/api/seller-goods"):
            return httpx.Response(
                status_code=200,
                json={
                    "rows": [
                        {
                            "id_goods": 102249445,
                            "name_goods": "Steam top up",
                            "price": 7.0,
                            "currency": "USD",
                            "visible": 1,
                        }
                    ]
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("102249445")
    client.close()

    assert offer is not None
    assert offer["id_goods"] == 102249445
    assert offer["name_goods"] == "Steam top up"


def test_ggsel_get_offer_by_id_paginates_seller_goods() -> None:
    seen_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/products/102249445/data"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        if request.url.path.endswith("/api_sellers/api/product/102249445/data"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        if request.url.path.endswith("/api_sellers/api/products/list"):
            return httpx.Response(status_code=404, json={"error": "not found"})
        if request.url.path.endswith("/api_sellers/api/seller-goods"):
            body = json.loads(request.content.decode() or "{}")
            seen_bodies.append(body)
            if body.get("page") == 2:
                return httpx.Response(
                    status_code=200,
                    json={
                        "retval": 0,
                        "page": 2,
                        "pages": 2,
                        "rows": [{"id_goods": 102249445, "name_goods": "Steam RU+CIS"}],
                    },
                )
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "page": 1,
                    "pages": 2,
                    "rows": [{"id_goods": 111, "name_goods": "Other product"}],
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("102249445")
    client.close()

    assert offer is not None
    assert offer["id_goods"] == 102249445
    assert seen_bodies == [{}, {"page": 2}]


def test_ggsel_get_offer_by_id_surfaces_auth_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401, text="Unauthorized")

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError, match="HTTP 401"):
        client.get_offer_by_id("102249445")
    client.close()


def test_ggsel_get_offer_by_id_prefers_product_data_endpoint() -> None:
    captured_paths: list[str] = []
    captured_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        captured_queries.append(request.url.query.decode())
        if request.url.path.endswith("/api_sellers/api/products/5850465/data"):
            return httpx.Response(
                status_code=200,
                json={
                    "product": {
                        "id": 5850465,
                        "name": "Playstation testing",
                        "price": "7.15",
                        "currency": "USD",
                        "options": [
                            {
                                "label": "Gift Card Amount",
                                "variants": [
                                    {"variant_id": 19472011, "text": "250 TRY"},
                                    {"variant_id": 19472020, "text": "500 TRY"},
                                ],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("5850465")
    client.close()

    assert offer is not None
    assert offer["product_id"] == 5850465
    assert offer["options"][0]["variants"][1]["text"] == "500 TRY"
    assert captured_paths == ["/api_sellers/api/products/5850465/data"]
    assert captured_queries == [""]


def test_ggsel_set_offer_price_uses_bulk_endpoint_and_waits_for_task() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            captured["price_query"] = dict(request.url.params)
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"taskId": "ggsel-task-123"})
        if request.url.path.endswith("/api_sellers/api/product/edit/UpdateProductsTaskStatus"):
            captured["status_query"] = dict(request.url.params)
            return httpx.Response(
                status_code=200,
                json={
                    "TaskId": "ggsel-task-123",
                    "Status": 3,
                    "SuccessCount": 1,
                    "ErrorCount": 0,
                    "TotalCount": 1,
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(offer_id="5850465", price=12.34, currency="USD")
    client.close()

    assert result.get("status") == "ok"
    assert result.get("task_id") == "ggsel-task-123"
    assert captured["price_query"]["token"] == "secret"
    assert captured["price_json"] == [{"product_id": 5850465, "price": 12.34}]
    assert captured["status_query"]["taskId"] == "ggsel-task-123"


def test_ggsel_set_offer_price_supports_variants_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="5850465",
        price=7.15,
        currency="USD",
        variants=[{"variant_id": 19472020, "rate": 6.60, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert captured["price_json"] == [
        {
            "product_id": 5850465,
            "price": 7.15,
            "variants": [{"variant_id": 19472020, "rate": 6.6, "type": "priceplus"}],
        }
    ]


def test_ggsel_set_offer_price_uses_apilogin_token_when_seller_id_is_configured() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/apilogin"):
            payload = json.loads(request.content.decode("utf-8"))
            captured["apilogin_payload"] = payload
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "token": "ggsel-token-from-apilogin",
                    "seller_id": 123456,
                    "valid_thru": "2099-01-01T00:00:00Z",
                },
            )
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            captured["price_query"] = dict(request.url.params)
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(
        base_url="https://seller.ggsel.com",
        api_key="seller-api-key",
        seller_id="123456",
    )
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(
        offer_id="102249445",
        price=610.55,
        currency="RUB",
        variants=[{"variant_id": 18944509, "rate": 0.0, "type": 2}],
    )
    client.close()

    assert result == {"status": "ok"}
    assert captured["apilogin_payload"]["seller_id"] == 123456
    assert isinstance(captured["apilogin_payload"]["timestamp"], int)
    assert isinstance(captured["apilogin_payload"]["sign"], str)
    assert captured["price_query"]["token"] == "ggsel-token-from-apilogin"
    assert captured["price_json"] == [
        {
            "product_id": 102249445,
            "price": 610.55,
            "variants": [{"variant_id": 18944509, "rate": 0.0, "type": "priceplus"}],
        }
    ]


def test_ggsel_set_offer_price_refreshes_apilogin_token_after_401() -> None:
    state = {"apilogin_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/apilogin"):
            state["apilogin_calls"] += 1
            token = "ggsel-token-a" if state["apilogin_calls"] == 1 else "ggsel-token-b"
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "token": token,
                    "seller_id": 123456,
                    "valid_thru": "2099-01-01T00:00:00Z",
                },
            )
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            token = request.url.params.get("token")
            if token == "ggsel-token-a":
                return httpx.Response(status_code=401, json={"error": "unauthorized"})
            if token == "ggsel-token-b":
                return httpx.Response(status_code=200, json={"status": "ok"})
            return httpx.Response(status_code=400, json={"error": "unexpected token"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(
        base_url="https://seller.ggsel.com",
        api_key="seller-api-key",
        seller_id="123456",
    )
    _attach_mock_transport(client, httpx.MockTransport(handler))

    result = client.set_offer_price(offer_id="102249445", price=610.55, currency="RUB")
    client.close()

    assert result == {"status": "ok"}
    assert state["apilogin_calls"] == 2


def test_ggsel_set_offer_price_auth_error_explains_seller_id_requirement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            return httpx.Response(status_code=401, text="Unauthorized")
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = GGSELClient(base_url="https://seller.ggsel.com", api_key="browser-jwt")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    with pytest.raises(APIRequestError, match="GGSEL_SELLER_ID"):
        client.set_offer_price(offer_id="102249445", price=610.55, currency="RUB")
    client.close()


def test_digiseller_get_offer_by_id_uses_products_list_when_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/products/list"):
            return httpx.Response(
                status_code=200,
                json={
                    "items": [
                        {
                            "product_id": "5850465",
                            "name": "Playstation testing 250 TL",
                            "price": "7.00",
                            "currency": "USD",
                        }
                    ]
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("5850465")
    client.close()

    assert offer is not None
    assert str(offer["product_id"]) == "5850465"


def test_digiseller_list_offers_normalizes_products_list_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/products/list"):
            return httpx.Response(
                status_code=200,
                json={
                    "data": {
                        "items": [
                            {"product_id": "5850465", "name": "PSN 250 TRY"},
                            {"product_id": "5850466", "name": "PSN 500 TRY"},
                        ]
                    }
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offers = client.list_offers()
    client.close()

    ids = {str(item.get("product_id")) for item in offers}
    assert ids == {"5850465", "5850466"}


def test_digiseller_get_offer_by_id_uses_products_data_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/products/5850465/data"):
            return httpx.Response(
                status_code=200,
                json={
                    "retval": 0,
                    "retdesc": "",
                    "product": {
                        "id": 5850465,
                        "name": "Playstation testing",
                        "price": 7.0,
                        "currency": "USD",
                    },
                },
            )
        return httpx.Response(status_code=404, json={"error": "unexpected path"})

    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    _attach_mock_transport(client, httpx.MockTransport(handler))

    offer = client.get_offer_by_id("5850465")
    client.close()

    assert offer is not None
    assert str(offer["id"]) == "5850465"
    assert str(offer["product_id"]) == "5850465"
