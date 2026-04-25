from __future__ import annotations

from typing import Any

from app.clients.base import BaseAPIClient


class DigisellerClient(BaseAPIClient):
    """
    Reference docs: https://api.digiseller.com/swagger/ui/index

    Confirmed endpoint mapping:
    - POST /api/product/edit/prices
    - POST /api/seller-sells/v2
    - GET /api/purchase/info/{invoiceID}
    - POST /api/debates/v2 (requires correspondence ID mapping)

    Product creation and base listing edits are intentionally disabled. Marketplace
    offers must be created manually and linked through /offers/manual-link.
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

    def list_offers(self) -> list[dict[str, Any]]:
        # TODO: Swagger exposes /api/products/list for ID-based lookup; a dedicated
        # "list all seller offers" endpoint is not confirmed in the provided docs.
        raise NotImplementedError(
            "Digiseller list_offers is not implemented. "
            "Please provide endpoint details for listing all seller offers."
        )

    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise NotImplementedError(
            "Digiseller create_offer is disabled. Create offers manually in the marketplace "
            "and link them via /offers/manual-link."
        )

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _ = (offer_id, payload)
        raise NotImplementedError(
            "Digiseller update_offer is disabled. Edit marketplace listings manually; "
            "this app only links existing offers."
        )

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
