from __future__ import annotations

from typing import Any

from app.clients.base import APIClientError, BaseAPIClient


class GEngineClient(BaseAPIClient):
    """
    Reference docs: https://api.g-engine.net/v2.1/docs

    Confirmed endpoint mapping (from OpenAPI):
    - GET /v2.1/users/balance
    - GET /v2.1/shop/products
    - GET /v2.1/shop/denominations/{product_id}
    - POST /v2.1/shop/orders
    - GET /v2.1/shop/orders/{order_id}
    """

    API_PREFIX = "/v2.1"

    def __init__(self, base_url: str, api_key: str | None, timeout: float = 20.0) -> None:
        super().__init__(
            service_name="G-Engine",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )

    def _build_path(self, endpoint: str) -> str:
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        if self.base_url.endswith(self.API_PREFIX):
            return endpoint
        return f"{self.API_PREFIX}{endpoint}"

    def _auth_headers(self) -> dict[str, str]:
        api_key = self._require_api_key()
        return {"X-API-Key": api_key}

    @staticmethod
    def _extract_dict_items(value: object) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _resolve_product_id(payload: dict[str, Any]) -> str | None:
        for key in ("id", "product_id", "productId", "sku"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _extract_base_products(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("items", "products"):
                items = self._extract_dict_items(data.get(key))
                if items:
                    return items
        items = self._extract_dict_items(data)
        if items:
            return items
        return []

    def _extract_denominations(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("denominations", "items"):
                items = self._extract_dict_items(data.get(key))
                if items:
                    return items
        for key in ("denominations", "items"):
            items = self._extract_dict_items(response.get(key))
            if items:
                return items
        items = self._extract_dict_items(data)
        if items:
            return items
        return []

    def get_balance(self) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=self._build_path("/users/balance"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine balance response format.")
        return response

    def list_products(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="GET",
            path=self._build_path("/shop/products"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine products response format.")

        base_products = self._extract_base_products(response)
        expanded_products: list[dict[str, Any]] = []

        for product in base_products:
            product_id = self._resolve_product_id(product)
            if not product_id:
                expanded_products.append(product)
                continue

            try:
                detail_response = self.get_product(product_id)
            except APIClientError:
                expanded_products.append(product)
                continue

            denominations = self._extract_denominations(detail_response)
            if not denominations:
                expanded_products.append(product)
                continue

            for index, denomination in enumerate(denominations, start=1):
                combined = {**product, **denomination}
                combined.setdefault("product_id", product_id)
                if combined.get("id") is None:
                    combined["id"] = f"{product_id}:{index}"
                expanded_products.append(combined)

        return expanded_products

    def get_product(self, product_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=self._build_path(f"/shop/denominations/{product_id}"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine product response format.")
        return response

    def create_order(self, product_id: str, quantity: int) -> dict[str, Any]:
        order_payload = {
            "items": [
                {
                    "id": int(product_id),
                    "quantity": quantity,
                }
            ]
        }
        response = self._request_json(
            method="POST",
            path=self._build_path("/shop/orders"),
            headers=self._auth_headers(),
            json_body=order_payload,
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine order response format.")
        return response

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=self._build_path(f"/shop/orders/{order_id}"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine order status response format.")
        return response
