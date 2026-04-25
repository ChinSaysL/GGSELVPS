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

    def list_product_denominations(
        self,
        product_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        detail_response = self.get_product(product_id)
        denominations = self._extract_denominations(detail_response)
        base_product = self._extract_product_detail_base(
            detail_response,
            product_id=product_id,
            fallback_title=fallback_title,
            source_slug=source_slug,
        )

        if not denominations:
            return [base_product]

        expanded_products: list[dict[str, Any]] = []
        for index, denomination in enumerate(denominations, start=1):
            combined = {**base_product, **denomination}
            combined.setdefault("product_id", product_id)
            combined.setdefault("base_product_id", product_id)
            combined.setdefault("source_slug", source_slug)
            if combined.get("id") is None:
                combined["id"] = f"{product_id}:{index}"
            expanded_products.append(combined)
        return expanded_products

    def _extract_product_detail_base(
        self,
        response: dict[str, Any],
        *,
        product_id: str,
        fallback_title: str | None,
        source_slug: str | None,
    ) -> dict[str, Any]:
        data = response.get("data")
        detail = data if isinstance(data, dict) else response
        base: dict[str, Any] = {
            "product_id": product_id,
            "base_product_id": product_id,
            "source_slug": source_slug,
        }

        if isinstance(detail, dict):
            for key, value in detail.items():
                if isinstance(value, (dict, list)):
                    continue
                if key in {"denominations", "items"}:
                    continue
                base[key] = value

        if fallback_title:
            base.setdefault("title", fallback_title)
            base.setdefault("name", fallback_title)
        return base

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
