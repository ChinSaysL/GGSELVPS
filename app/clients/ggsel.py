from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.clients.base import APIClientConfigError, APIRequestError, BaseAPIClient


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
    MAX_LIST_PAGES = 50
    _TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        seller_id: str | None = None,
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
        self.seller_id = seller_id
        self._task_poll_timeout_seconds = task_poll_timeout_seconds
        self._task_poll_interval_seconds = task_poll_interval_seconds
        self._access_token: str | None = None
        self._access_token_valid_thru: datetime | None = None
        self._last_apilogin_timestamp: int | None = None
        self._last_apilogin_error: str | None = None

    def _build_path(self, endpoint: str) -> str:
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        if self.base_url.endswith(self.API_PREFIX):
            return endpoint
        return f"{self.API_PREFIX}{endpoint}"

    def _auth_query_params(self) -> dict[str, str]:
        token = self._get_access_token()
        return {"token": token}

    @staticmethod
    def _parse_valid_thru(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _cached_token_is_usable(self) -> bool:
        if not self._access_token:
            return False
        if self._access_token_valid_thru is None:
            return True
        return self._access_token_valid_thru > datetime.now(timezone.utc) + self._TOKEN_REFRESH_MARGIN

    def _next_apilogin_timestamp(self) -> int:
        timestamp = int(datetime.now(timezone.utc).timestamp())
        if self._last_apilogin_timestamp is not None and timestamp <= self._last_apilogin_timestamp:
            timestamp = self._last_apilogin_timestamp + 1
        self._last_apilogin_timestamp = timestamp
        return timestamp

    def _fetch_access_token_via_apilogin(self) -> tuple[str, datetime | None]:
        if not self.seller_id:
            raise APIClientConfigError(
                "GGSEL_SELLER_ID is required to auto-refresh GGSEL seller API tokens."
            )
        try:
            seller_id = int(self.seller_id)
        except ValueError as exc:
            raise APIClientConfigError("GGSEL_SELLER_ID must be an integer.") from exc

        api_key = self._require_api_key()
        timestamp = self._next_apilogin_timestamp()
        sign = hashlib.sha256(f"{api_key}{timestamp}".encode("utf-8")).hexdigest()
        response = self._request_json(
            method="POST",
            path=self._build_path("/api/apilogin"),
            json_body={
                "seller_id": seller_id,
                "timestamp": timestamp,
                "sign": sign,
            },
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL /api/apilogin returned unexpected response format.")

        retval = response.get("retval")
        token_raw = response.get("token")
        token = token_raw.strip() if isinstance(token_raw, str) else ""
        if retval not in (0, "0") or not token:
            desc = str(response.get("desc") or response.get("retdesc") or "").strip()
            if not desc:
                desc = "unknown error"
            raise APIRequestError(f"GGSEL /api/apilogin failed: {desc}")

        return token, self._parse_valid_thru(response.get("valid_thru"))

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        api_key = self._require_api_key()
        if not self.seller_id:
            return api_key

        if force_refresh:
            self._access_token = None
            self._access_token_valid_thru = None

        if self._cached_token_is_usable():
            return self._access_token or api_key

        try:
            token, valid_thru = self._fetch_access_token_via_apilogin()
        except Exception as exc:
            self._last_apilogin_error = str(exc)
            return api_key

        self._access_token = token
        self._access_token_valid_thru = valid_thru
        self._last_apilogin_error = None
        return token

    @staticmethod
    def _extract_offer_id(item: dict[str, Any]) -> str | None:
        for key in (
            "id",
            "product_id",
            "productId",
            "productID",
            "goods_id",
            "goodsId",
            "good_id",
            "goodId",
            "id_goods",
            "id_good",
            "id_product",
        ):
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _collect_offer_dicts(value: object) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                collected.extend(GGSELClient._collect_offer_dicts(item))
            return collected
        if not isinstance(value, dict):
            return collected

        if GGSELClient._extract_offer_id(value) is not None:
            collected.append(value)

        for nested in value.values():
            collected.extend(GGSELClient._collect_offer_dicts(nested))
        return collected

    @staticmethod
    def _is_lookup_miss(exc: APIRequestError) -> bool:
        message = str(exc)
        return "HTTP 400" in message or "HTTP 404" in message

    @staticmethod
    def _payload_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            try:
                return int(str(value))
            except ValueError:
                continue
        return None

    @classmethod
    def _has_next_offer_page(cls, response: dict[str, Any] | list[Any], requested_page: int) -> bool:
        if not isinstance(response, dict):
            return False
        has_next = response.get("has_next_page")
        if isinstance(has_next, bool):
            return has_next
        current_page = cls._payload_int(response, ("page",)) or requested_page
        total_pages = cls._payload_int(response, ("total_pages", "pages"))
        return total_pages is not None and current_page < total_pages

    def _request_offer_list_page(
        self,
        *,
        method: str,
        endpoint: str,
        page: int,
        body_template: dict[str, Any] | None,
    ) -> dict[str, Any] | list[Any]:
        params = self._auth_query_params()
        body = dict(body_template) if body_template is not None else None
        if page > 1:
            if method.upper() == "GET":
                params = {**params, "page": str(page)}
            else:
                body = {} if body is None else body
                body.setdefault("page", page)
        return self._request_json(
            method=method,
            path=self._build_path(endpoint),
            params=params,
            json_body=body,
        )

    def list_offers(self) -> list[dict[str, Any]]:
        attempts: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
            ("GET", "/api/products/list", None),
            ("POST", "/api/seller-goods", {}),
        )
        merged: dict[str, dict[str, Any]] = {}
        first_unexpected_error: APIRequestError | None = None

        for method, endpoint, body in attempts:
            page = 1
            while page <= self.MAX_LIST_PAGES:
                try:
                    response = self._request_offer_list_page(
                        method=method,
                        endpoint=endpoint,
                        page=page,
                        body_template=body,
                    )
                except APIRequestError as exc:
                    if not self._is_lookup_miss(exc) and first_unexpected_error is None:
                        first_unexpected_error = exc
                    break

                for item in self._collect_offer_dicts(response):
                    offer_id = self._extract_offer_id(item)
                    if offer_id is None:
                        continue
                    merged.setdefault(offer_id, item)

                if not self._has_next_offer_page(response, page):
                    break
                page += 1

        if not merged and first_unexpected_error is not None:
            raise first_unexpected_error

        return list(merged.values())

    @staticmethod
    def _matches_offer_id(item: dict[str, Any], offer_id: str) -> bool:
        candidates = (
            "id",
            "goods_id",
            "goodsId",
            "good_id",
            "goodId",
            "product_id",
            "productId",
            "productID",
            "id_goods",
            "id_good",
            "id_product",
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

        direct_paths = (
            f"/api/products/{normalized}/data",
            f"/api/product/{normalized}/data",
        )
        first_unexpected_error: APIRequestError | None = None
        for path in direct_paths:
            try:
                response = self._request_json(
                    method="GET",
                    path=self._build_path(path),
                    headers={"Accept": "application/json"},
                )
            except APIRequestError as exc:
                if not self._is_lookup_miss(exc) and first_unexpected_error is None:
                    first_unexpected_error = exc
                response = None

            if isinstance(response, dict):
                product = response.get("product")
                if isinstance(product, dict):
                    product.setdefault("product_id", product.get("id"))
                    product.setdefault("id_goods", product.get("id"))
                    return product
                for item in self._collect_offer_dicts(response):
                    if self._matches_offer_id(item, normalized):
                        return item
                if self._matches_offer_id(response, normalized):
                    return response

        for item in self.list_offers():
            if self._matches_offer_id(item, normalized):
                return item
        if first_unexpected_error is not None:
            raise first_unexpected_error
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

    def _auth_error_hint(self) -> str:
        if self.seller_id:
            if self._last_apilogin_error:
                return (
                    "GGSEL seller API token was rejected; /api/apilogin fallback also failed: "
                    f"{self._last_apilogin_error}"
                )
            return "GGSEL seller API token was rejected even after /api/apilogin refresh."
        return (
            "GGSEL seller API token was rejected. Set GGSEL_SELLER_ID with a seller API key "
            "in GGSEL_API_KEY, or provide a valid short-lived GGSEL seller API token."
        )

    def _request_price_update(
        self,
        *,
        request_payload: list[dict[str, Any]],
        token: str,
    ) -> dict[str, Any]:
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
            detail = self._response_error_excerpt(response)
            suffix = f": {detail}" if detail else ""
            if response.status_code in {401, 403}:
                suffix = f"{suffix} ({self._auth_error_hint()})"
            raise APIRequestError(
                "GGSEL returned HTTP "
                f"{response.status_code} for POST /api_sellers/api/product/edit/prices{suffix}"
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

    def _fetch_price_update_task_status(self, *, task_id: str, token: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=self._build_path("/api/product/edit/UpdateProductsTaskStatus"),
            params={"taskId": task_id, "token": token},
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL task status returned unexpected response format.")
        return response

    def _wait_for_price_update_task(self, *, task_id: str, token: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._task_poll_timeout_seconds
        last_status_payload: dict[str, Any] | None = None

        while time.monotonic() <= deadline:
            status_payload = self._fetch_price_update_task_status(task_id=task_id, token=token)
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

    def set_offer_price(
        self,
        offer_id: str,
        price: float,
        currency: str,
        *,
        variants: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        _ = currency
        request_item: dict[str, Any] = {
            "product_id": int(offer_id),
            "price": float(price),
        }
        if variants:
            request_item["variants"] = variants
        request_payload: list[dict[str, Any]] = [request_item]
        last_auth_error: APIRequestError | None = None
        refresh_rounds = (False, True) if self.seller_id else (False,)
        for force_refresh in refresh_rounds:
            token = self._get_access_token(force_refresh=force_refresh)
            try:
                response = self._request_price_update(
                    request_payload=request_payload,
                    token=token,
                )
            except APIRequestError as exc:
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    last_auth_error = exc
                    continue
                raise

            task_id = self._extract_task_id(response)
            if task_id:
                task_status = self._wait_for_price_update_task(task_id=task_id, token=token)
                task_status.setdefault("task_id", task_id)
                return task_status
            return response

        if last_auth_error is not None:
            raise last_auth_error
        response = self._request_price_update(
            request_payload=request_payload,
            token=self._get_access_token(),
        )
        task_id = self._extract_task_id(response)
        if task_id:
            task_status = self._wait_for_price_update_task(
                task_id=task_id,
                token=self._get_access_token(),
            )
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
