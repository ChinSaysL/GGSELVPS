from __future__ import annotations

from typing import Any

from app.clients.base import BaseAPIClient


class GGSELClient(BaseAPIClient):
    """
    Reference docs: https://seller.ggsel.com/docs/en/seller-api-v-1

    Confirmed endpoint mapping:
    - POST /api_sellers/api/seller-goods
    - GET /api_sellers/api/seller-last-sales
    - GET /api_sellers/api/purchase/info/:invoice_id

    Unconfirmed generic operations remain TODO to avoid guessing.
    """

    API_PREFIX = "/api_sellers"

    def __init__(self, base_url: str, api_key: str | None, timeout: float = 20.0) -> None:
        super().__init__(
            service_name="GGSEL",
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

    def _auth_query_params(self) -> dict[str, str]:
        api_key = self._require_api_key()
        return {"token": api_key}

    def list_offers(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="POST",
            path=self._build_path("/api/seller-goods"),
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

    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        # TODO: Seller API docs used here do not expose a generic "create offer" endpoint.
        raise NotImplementedError(
            "GGSEL create_offer is not implemented. "
            "Please provide the exact GGSEL endpoint for offer creation."
        )

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _ = (offer_id, payload)
        # TODO: Seller API docs used here do not expose a generic "update offer" endpoint.
        raise NotImplementedError(
            "GGSEL update_offer is not implemented. "
            "Please provide the exact GGSEL endpoint for offer updates."
        )

    def set_offer_enabled(self, offer_id: str, enabled: bool) -> dict[str, Any]:
        _ = (offer_id, enabled)
        # TODO: Seller API docs used here do not expose an explicit enable/disable endpoint.
        raise NotImplementedError(
            "GGSEL set_offer_enabled is not implemented. "
            "Please provide the exact GGSEL endpoint or field to toggle availability."
        )

    def set_offer_price(self, offer_id: str, price: float, currency: str) -> dict[str, Any]:
        _ = (offer_id, price, currency)
        # TODO: The endpoint path is known, but request schema for a single-offer update
        # needs confirmation from API docs/examples before implementation.
        raise NotImplementedError(
            "GGSEL set_offer_price is not implemented. "
            "Please provide the exact price update payload schema."
        )

    def get_latest_sales(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="GET",
            path=self._build_path("/api/seller-last-sales"),
            params=self._auth_query_params(),
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
            path=self._build_path(f"/api/purchase/info/{order_id}"),
            params=self._auth_query_params(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected GGSEL order response format.")
        return response

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        _ = (order_id, buyer_id, message)
        # TODO: Endpoint is known, but docs indicate chat/correspondence identifiers are
        # required; mapping from marketplace order to chat ID is not yet specified.
        raise NotImplementedError(
            "GGSEL send_buyer_message is not implemented. "
            "Please provide how to resolve correspondence ID from order data."
        )
