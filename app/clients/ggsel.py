from __future__ import annotations

import time
from typing import Any

from app.clients.base import APIRequestError, BaseAPIClient


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

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout: float = 20.0,
        task_poll_timeout_seconds: int = 120,
        task_poll_interval_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            service_name="GGSEL",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        self._task_poll_timeout_seconds = task_poll_timeout_seconds
        self._task_poll_interval_seconds = task_poll_interval_seconds

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

    @staticmethod
    def _matches_offer_id(item: dict[str, Any], offer_id: str) -> bool:
        candidates = (
            "id",
            "goods_id",
            "good_id",
            "product_id",
        )
        normalized_offer_id = offer_id.strip()
        for key in candidates:
            value = item.get(key)
            if value is None:
                continue
            if str(value).strip() == normalized_offer_id:
                return True
        return False

    def get_offer_by_id(self, offer_id: str) -> dict[str, Any] | None:
        normalized = offer_id.strip()
        if not normalized:
            return None

        for item in self.list_offers():
            if self._matches_offer_id(item, normalized):
                return item
        return None

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

    @staticmethod
    def _extract_task_id(response: dict[str, Any]) -> str | None:
        for key in ("taskId", "TaskId", "task_id", "task"):
            value = response.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _parse_task_status(value: object) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return None

    @staticmethod
    def _task_error_descriptions(task_status: dict[str, Any]) -> str:
        raw_errors = task_status.get("ErrorsDescriptions")
        if raw_errors is None:
            raw_errors = task_status.get("errorsDescriptions")
        if raw_errors is None:
            raw_errors = task_status.get("errors")

        if isinstance(raw_errors, dict):
            dict_parts = [
                f"{str(key).strip()}: {str(value).strip()}"
                for key, value in raw_errors.items()
                if str(value).strip()
            ]
            return "; ".join(dict_parts)

        if isinstance(raw_errors, list):
            list_parts: list[str] = []
            for item in raw_errors:
                if isinstance(item, dict):
                    key = str(item.get("Key") or item.get("key") or "").strip()
                    value = str(item.get("Value") or item.get("value") or "").strip()
                    if key and value:
                        list_parts.append(f"{key}: {value}")
                    elif value:
                        list_parts.append(value)
            return "; ".join(list_parts)

        if raw_errors is None:
            return ""
        return str(raw_errors).strip()

    def _request_price_update(self, *, request_payload: list[dict[str, Any]]) -> dict[str, Any]:
        token = self._require_api_key()
        try:
            response = self.http.request(
                method="POST",
                url=self._build_path("/api/product/edit/prices"),
                params={"token": token},
                json=request_payload,
            )
        except Exception as exc:  # noqa: BLE001
            raise APIRequestError("GGSEL request failed: POST /api_sellers/api/product/edit/prices") from exc

        if response.is_error:
            raise APIRequestError(
                f"GGSEL returned HTTP {response.status_code} for POST /api_sellers/api/product/edit/prices"
            )

        try:
            parsed = response.json()
        except ValueError:
            task_id = response.text.strip()
            if not task_id:
                return {"status": "ok"}
            if task_id.startswith("<") and "html" in task_id.lower():
                raise APIRequestError(
                    "GGSEL returned unexpected HTML for POST /api_sellers/api/product/edit/prices"
                )
            return {"status": "ok", "task_id": task_id}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            token_value = parsed.strip()
            if token_value:
                return {"status": "ok", "task_id": token_value}
            return {"status": "ok"}
        if isinstance(parsed, list):
            return {"status": "ok", "items": parsed}
        return {"status": "ok", "response": parsed}

    def _fetch_price_update_task_status(self, *, task_id: str) -> dict[str, Any]:
        token = self._require_api_key()
        response = self._request_json(
            method="GET",
            path=self._build_path("/api/product/edit/UpdateProductsTaskStatus"),
            params={"taskId": task_id, "token": token},
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL task status returned unexpected response format.")
        return response

    def _wait_for_price_update_task(self, *, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._task_poll_timeout_seconds
        last_status_payload: dict[str, Any] | None = None

        while time.monotonic() <= deadline:
            status_payload = self._fetch_price_update_task_status(task_id=task_id)
            last_status_payload = status_payload
            status_code = self._parse_task_status(
                status_payload.get("Status", status_payload.get("status"))
            )

            if status_code == 3:
                error_count_raw = status_payload.get("ErrorCount", status_payload.get("errorCount", 0))
                try:
                    error_count = int(str(error_count_raw))
                except ValueError:
                    error_count = 0
                if error_count > 0:
                    error_text = self._task_error_descriptions(status_payload) or "unknown task failure"
                    raise APIRequestError(
                        f"GGSEL price update task completed with errors ({task_id}): {error_text}"
                    )
                success_payload = dict(status_payload)
                success_payload.setdefault("status", "ok")
                success_payload.setdefault("task_id", task_id)
                return success_payload
            if status_code == 2:
                error_text = self._task_error_descriptions(status_payload) or "unknown task failure"
                raise APIRequestError(
                    f"GGSEL price update task failed ({task_id}): {error_text}"
                )

            time.sleep(self._task_poll_interval_seconds)

        if last_status_payload is not None:
            status_code = self._parse_task_status(
                last_status_payload.get("Status", last_status_payload.get("status"))
            )
            if status_code in (0, 1):
                pending_payload = dict(last_status_payload)
                pending_payload["status"] = "pending"
                pending_payload["task_id"] = task_id
                pending_payload["last_status"] = status_code
                return pending_payload
            raise APIRequestError(
                f"GGSEL price update task timed out ({task_id}), last status={status_code}"
            )
        raise APIRequestError(f"GGSEL price update task timed out ({task_id}).")

    def set_offer_price(self, offer_id: str, price: float, currency: str) -> dict[str, Any]:
        _ = currency
        request_payload: list[dict[str, Any]] = [
            {
                "product_id": int(offer_id),
                "price": float(price),
            }
        ]
        response = self._request_price_update(request_payload=request_payload)
        task_id = self._extract_task_id(response)
        if task_id:
            task_status = self._wait_for_price_update_task(task_id=task_id)
            task_status.setdefault("task_id", task_id)
            return task_status
        return response

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
