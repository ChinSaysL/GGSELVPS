from __future__ import annotations

from typing import Any, cast

from app.clients.base import APIClientConfigError, BaseAPIClient


class DigisellerClient(BaseAPIClient):
    """
    Reference docs: https://api.digiseller.com/swagger/ui/index

    Confirmed endpoint mapping:
    - POST /api/product/create/uniquefixed
    - POST /api/product/edit/base/{product_id}
    - POST /api/product/edit/prices
    - POST /api/seller-sells/v2
    - GET /api/purchase/info/{invoiceID}
    - POST /api/debates/v2 (requires correspondence ID mapping)
    """

    def __init__(self, base_url: str, api_key: str | None, timeout: float = 20.0) -> None:
        super().__init__(
            service_name="Digiseller",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )

    def _auth_query_params(self) -> dict[str, str]:
        api_key = self._require_api_key()
        return {"token": api_key}

    @staticmethod
    def _localized_text(value: object, fallback: str) -> list[dict[str, str]]:
        if isinstance(value, list):
            localized: list[dict[str, str]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                locale = str(item.get("locale", "")).strip()
                text = str(item.get("value", "")).strip()
                if not locale or not text:
                    continue
                localized.append({"locale": locale, "value": text})
            if localized:
                return localized
        return [{"locale": "en-US", "value": fallback}]

    @staticmethod
    def _resolve_categories(payload: dict[str, Any]) -> list[dict[str, int]]:
        categories_payload = payload.get("categories")
        if isinstance(categories_payload, list):
            categories: list[dict[str, int]] = []
            for item in categories_payload:
                if not isinstance(item, dict):
                    continue
                owner = item.get("owner")
                category_id = item.get("category_id")
                if owner in {None, ""} or category_id in {None, ""}:
                    continue
                owner_value = int(cast(str | int | float, owner))
                category_value = int(cast(str | int | float, category_id))
                categories.append({"owner": owner_value, "category_id": category_value})
            if categories:
                return categories

        category_id = payload.get("category_id")
        if category_id in {None, ""}:
            raise APIClientConfigError(
                "Digiseller category is required. Set DIGISELLER_CATEGORY_ID in environment "
                "or provide categories in payload."
            )

        owner_raw = payload.get("category_owner", 3)
        owner = int(cast(str | int | float, owner_raw))
        return [{"owner": owner, "category_id": int(cast(str | int | float, category_id))}]

    def _build_product_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        description = str(payload.get("description", "")).strip()
        if not title:
            raise APIClientConfigError("Digiseller payload is missing title.")
        if not description:
            raise APIClientConfigError("Digiseller payload is missing description.")

        price = payload.get("price")
        currency = str(payload.get("currency", "")).upper().strip()
        if price is None:
            raise APIClientConfigError("Digiseller payload is missing price.")
        if not currency:
            raise APIClientConfigError("Digiseller payload is missing currency.")

        dto: dict[str, Any] = {
            "content_type": str(payload.get("content_type", "Text")),
            "categories": self._resolve_categories(payload),
            "name": self._localized_text(payload.get("title_localized"), title),
            "description": self._localized_text(payload.get("description_localized"), description),
            "price": {
                "price": float(price),
                "currency": currency,
            },
            "enabled": bool(payload.get("enabled", False)),
        }
        return dto

    def list_offers(self) -> list[dict[str, Any]]:
        # TODO: Swagger exposes /api/products/list for ID-based lookup; a dedicated
        # "list all seller offers" endpoint is not confirmed in the provided docs.
        raise NotImplementedError(
            "Digiseller list_offers is not implemented. "
            "Please provide endpoint details for listing all seller offers."
        )

    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path="/api/product/create/uniquefixed",
            params=self._auth_query_params(),
            json_body=self._build_product_payload(payload),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller create response format.")
        return response

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path=f"/api/product/edit/base/{offer_id}",
            params=self._auth_query_params(),
            json_body=self._build_product_payload(payload),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller update response format.")
        return response

    def set_offer_enabled(self, offer_id: str, enabled: bool) -> dict[str, Any]:
        _ = (offer_id, enabled)
        # TODO: Availability flag field name is not confirmed in the docs used here.
        raise NotImplementedError(
            "Digiseller set_offer_enabled is not implemented. "
            "Please provide the exact payload field for enable/disable."
        )

    def set_offer_price(self, offer_id: str, price: float, currency: str) -> dict[str, Any]:
        if currency.upper() != "RUB":
            raise NotImplementedError(
                "Digiseller set_offer_price currently supports RUB listings only in v1."
            )

        request_payload: list[dict[str, Any]] = [
            {
                "product_id": int(offer_id),
                "price": float(price),
            }
        ]
        response = self._request_json(
            method="POST",
            path="/api/product/edit/prices",
            params=self._auth_query_params(),
            json_body=request_payload,
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller price update response format.")
        return response

    def get_latest_sales(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="POST",
            path="/api/seller-sells/v2",
            params=self._auth_query_params(),
            json_body={},
        )
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    def get_order(self, order_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=f"/api/purchase/info/{order_id}",
            params=self._auth_query_params(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller order response format.")
        return response

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        _ = (order_id, buyer_id, message)
        # TODO: /api/debates/v2 requires correspondence ID (id_i). Mapping from
        # marketplace order to correspondence ID is not defined yet.
        raise NotImplementedError(
            "Digiseller send_buyer_message is not implemented. "
            "Please provide how to resolve correspondence ID from order data."
        )
