from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.clients import APIClientConfigError, APIRequestError, DigisellerClient, GEngineClient, GGSELClient


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
    with pytest.raises(NotImplementedError):
        client.send_buyer_message(order_id="1", buyer_id="2", message="hello")
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

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
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
            "variants": [{"variant_id": 19472020, "rate": 6.60, "type": 2}],
        }
    ]


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
