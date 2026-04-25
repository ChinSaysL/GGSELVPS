from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.clients import APIClientConfigError, DigisellerClient, GEngineClient, GGSELClient


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


def test_digiseller_set_offer_price_rub_only_for_v1() -> None:
    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    with pytest.raises(NotImplementedError):
        client.set_offer_price(offer_id="10", price=99.9, currency="USD")
    client.close()


def test_digiseller_listing_create_and_base_update_are_disabled() -> None:
    client = DigisellerClient(base_url="https://api.digiseller.com", api_key="secret")
    with pytest.raises(NotImplementedError):
        client.create_offer({"title": "x"})
    with pytest.raises(NotImplementedError):
        client.update_offer(offer_id="10", payload={"title": "x"})
    client.close()
