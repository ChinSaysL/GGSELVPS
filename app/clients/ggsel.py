from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

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
        # GGSEL API v1 has no dedicated offer-level enable/disable endpoint.
        # When enabling, individual variants are already controlled by set_offer_variant_state.
        # When disabling entirely, force-hide all variants as a safety net.
        if enabled:
            return {"status": "ok"}
        token = self._get_access_token()
        product = self.get_offer_by_id(offer_id)
        if not isinstance(product, dict):
            raise APIRequestError(f"GGSEL product {offer_id} not found for status update.")
        updated = 0
        for option in self._offer_options(product):
            option_id = self._extract_option_id(option)
            if option_id is None:
                continue
            for variant in self._option_variants(option):
                variant_id = self._extract_variant_id(variant)
                if variant_id is None:
                    continue
                current_default = self._extract_bool(variant, ("is_default", "default"))
                payload = self._build_variant_update_payload(
                    variant,
                    visible=False,
                    is_default=bool(current_default) if current_default is not None else False,
                )
                self._request_json(
                    method="POST",
                    path=self._build_path(f"/api/products/options/{option_id}/variants/{variant_id}"),
                    params={"token": token},
                    json_body=payload,
                )
                updated += 1
        return {"status": "ok", "variants_updated": updated}

    @staticmethod
    def _response_is_success(response: dict[str, Any]) -> bool:
        retval = response.get("retval")
        if retval is None:
            return True
        return retval in (0, "0")

    @staticmethod
    def _response_error_message(response: dict[str, Any]) -> str:
        errors = response.get("errors")
        if isinstance(errors, list):
            parts: list[str] = []
            for item in errors:
                if isinstance(item, dict):
                    code = str(item.get("code") or "").strip()
                    message = str(item.get("message") or "").strip()
                    if code and message:
                        parts.append(f"{code}: {message}")
                    elif message:
                        parts.append(message)
            if parts:
                return "; ".join(parts)
        if isinstance(errors, dict):
            parts = [
                f"{str(key).strip()}: {str(value).strip()}"
                for key, value in errors.items()
                if str(value).strip()
            ]
            if parts:
                return "; ".join(parts)

        for key in ("retdesc", "desc", "message"):
            value = response.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return "unknown api error"

    def _ensure_success_response(self, response: dict[str, Any], *, context: str) -> None:
        if self._response_is_success(response):
            return
        reason = self._response_error_message(response)
        raise APIRequestError(f"{context}: {reason}")

    @staticmethod
    def _extract_option_id(payload: object) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("id", "option_id", "optionId", "name"):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                return int(str(raw).strip())
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_variant_id(payload: object) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("variant_id", "variantId", "value", "id"):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                return int(str(raw).strip())
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_bool(payload: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
        for key in keys:
            raw = payload.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, int):
                return raw != 0
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
        return None

    @staticmethod
    def _first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _parse_int(value: object, *, default: int) -> int:
        if value is None:
            return default
        try:
            return int(str(value).strip())
        except ValueError:
            return default

    @staticmethod
    def _variant_type_name(value: object) -> str:
        if isinstance(value, int):
            return {
                0: "percentplus",
                1: "percentminus",
                2: "priceplus",
                3: "priceminus",
            }.get(value, "priceplus")

        text = str(value or "").strip()
        compact = text.replace("_", "").replace("-", "").replace(" ", "").lower()
        if compact in {"percentplus", "0"}:
            return "percentplus"
        if compact in {"percentminus", "1"}:
            return "percentminus"
        if compact in {"priceminus", "3"}:
            return "priceminus"
        return "priceplus"

    @classmethod
    def _normalize_price_update_variant(cls, variant: dict[str, Any]) -> dict[str, Any]:
        variant_id = cls._extract_variant_id(variant)
        if variant_id is None:
            raise APIRequestError("GGSEL variant price update is missing variant id.")
        rate = variant.get("rate", 0)
        return {
            "variant_id": int(variant_id),
            "rate": float(rate),
            "type": cls._variant_type_name(variant.get("type", "priceplus")),
        }

    @classmethod
    def _option_variants(cls, option: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("variants", "values", "items", "options"):
            value = option.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @classmethod
    def _offer_options(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        product = payload.get("product")
        if isinstance(product, dict):
            payload = product
        options = payload.get("options")
        if not isinstance(options, list):
            return []
        return [item for item in options if isinstance(item, dict)]

    def _list_product_option_ids(self, product_id: str, *, token: str) -> list[int]:
        try:
            response = self._request_json(
                method="GET",
                path=self._build_path(f"/api/products/options/list/{product_id}"),
                params={"token": token},
            )
        except APIRequestError as exc:
            if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                raise NotImplementedError("marketplace_disable_not_supported: GGSEL option listing is not supported.") from exc
            raise
            
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL options list returned unexpected response format.")
        self._ensure_success_response(response, context="GGSEL options/list failed")

        content = response.get("content")
        if not isinstance(content, list):
            return []
        
        option_ids: list[int] = []
        for item in content:
            option_id = self._extract_option_id(item)
            if option_id is not None:
                option_ids.append(option_id)
        return option_ids

    def _find_variant_payload_from_product_data(
        self,
        *,
        product_id: str,
        variant_id: int,
    ) -> tuple[int, dict[str, Any]] | None:
        product = self.get_offer_by_id(product_id)
        if not isinstance(product, dict):
            return None

        for option in self._offer_options(product):
            option_id = self._extract_option_id(option)
            if option_id is None:
                continue
            for variant in self._option_variants(option):
                if self._extract_variant_id(variant) == variant_id:
                    return option_id, variant
        return None

    def _get_product_option(self, option_id: int, *, token: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=self._build_path(f"/api/products/options/{option_id}"),
            params={"token": token},
        )
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL option details returned unexpected response format.")
        self._ensure_success_response(response, context="GGSEL options/details failed")

        content = response.get("content")
        if not isinstance(content, dict):
            raise APIRequestError("GGSEL option details missing content object.")
        return content

    def _find_variant_payload(
        self,
        *,
        product_id: str,
        variant_id: int,
        token: str,
    ) -> tuple[int, dict[str, Any]]:
        product_data_match = self._find_variant_payload_from_product_data(
            product_id=product_id,
            variant_id=variant_id,
        )
        if product_data_match is not None:
            return product_data_match

        option_ids = self._list_product_option_ids(product_id, token=token)
        for option_id in option_ids:
            option = self._get_product_option(option_id, token=token)
            for variant in self._option_variants(option):
                if self._extract_variant_id(variant) == variant_id:
                    return option_id, variant
        raise APIRequestError(f"GGSEL variant {variant_id} was not found on product {product_id}.")

    @classmethod
    def _build_variant_update_payload(
        cls,
        variant: dict[str, Any],
        *,
        visible: bool,
        is_default: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}

        name = variant.get("name")
        text = variant.get("text")
        if name is not None:
            payload["name"] = name
        if text is not None:
            payload["text"] = text

        rate = cls._first_payload_value(variant, ("rate", "modify_value", "modify_value_default"))
        if rate is None:
            rate = 0
        type_value = cls._first_payload_value(variant, ("type", "modify_type"))
        if type_value is None:
            type_value = "priceplus"

        payload["type"] = type_value
        payload["rate"] = rate
        payload["modify_type"] = variant.get("modify_type") or cls._variant_type_name(type_value)
        payload["modify_value"] = str(rate)
        if variant.get("modify") not in (None, ""):
            payload["modify"] = variant.get("modify")
        if variant.get("modify_value_default") not in (None, ""):
            payload["modify_value_default"] = variant.get("modify_value_default")
        if variant.get("num_in_stock") not in (None, ""):
            payload["num_in_stock"] = variant.get("num_in_stock")

        payload["default"] = is_default
        payload["visible"] = bool(visible)
        payload["order"] = cls._parse_int(variant.get("order"), default=1)
        return payload

    def set_offer_variant_state(
        self,
        offer_id: str,
        variant_id: int,
        *,
        visible: bool,
        is_default: bool | None = None,
    ) -> dict[str, Any]:
        token = self._get_access_token()
        option_id, variant = self._find_variant_payload(
            product_id=offer_id,
            variant_id=variant_id,
            token=token,
        )

        current_visible = self._extract_bool(variant, ("visible", "is_visible"))
        current_default = self._extract_bool(variant, ("is_default", "default"))
        target_default = bool(current_default) if is_default is None else bool(is_default)
        visible_matches = current_visible is not None and current_visible == bool(visible)
        default_matches = is_default is None or current_default == bool(is_default)
        if visible_matches and default_matches:
            return {"status": "ok", "unchanged": True}

        payload = self._build_variant_update_payload(
            variant,
            visible=visible,
            is_default=target_default,
        )
        response = self._request_json(
            method="POST",
            path=self._build_path(f"/api/products/options/{option_id}/variants/{variant_id}"),
            params={"token": token},
            json_body=payload,
        )
        if not isinstance(response, dict):
            raise APIRequestError("GGSEL variant update returned unexpected response format.")
        self._ensure_success_response(response, context="GGSEL variant visibility update failed")
        return {
            "status": "ok",
            "offer_id": offer_id,
            "option_id": option_id,
            "variant_id": variant_id,
            "visible": bool(visible),
            "is_default": target_default,
        }

    def set_offer_variant_visible(
        self,
        offer_id: str,
        variant_id: int,
        visible: bool,
    ) -> dict[str, Any]:
        return self.set_offer_variant_state(
            offer_id,
            variant_id,
            visible=visible,
            is_default=None,
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
            if list_parts:
                return "; ".join(list_parts)

        # Fallback if we completely missed the error structure
        _non_error_keys = {
            "status", "task_id", "taskid", "successcount", "totalcount", "errorcount",
        }
        fallback_errors = {
            str(k).strip(): str(v).strip()
            for k, v in task_status.items()
            if str(k).strip().lower() not in _non_error_keys and v not in (None, "")
        }
        if fallback_errors:
            return repr(fallback_errors)

        return ""

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

            # GGSEL uses Status=2 for "completed" (Digiseller uses 3).
            # Treat both as terminal; rely on ErrorCount to distinguish success from failure.
            if status_code in (2, 3):
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
            request_item["variants"] = [
                self._normalize_price_update_variant(variant)
                for variant in variants
                if isinstance(variant, dict)
            ]
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

    @classmethod
    def _extract_sales_items(cls, payload: object) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("rows", "sales", "items", "data", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = cls._extract_sales_items(value)
                if nested:
                    return nested
        return []

    @staticmethod
    def _normalize_sales_item(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        product = normalized.get("product")
        if isinstance(product, dict):
            for source_key, target_key in (
                ("id", "product_id"),
                ("name", "product_name"),
                ("price_usd", "amount_usd"),
                ("price_rub", "amount_rub"),
                ("price_eur", "amount_eur"),
                ("price_uah", "amount_uah"),
            ):
                value = product.get(source_key)
                if value not in (None, ""):
                    normalized.setdefault(target_key, value)
        return normalized

    def get_latest_sales(
        self,
        *,
        date_start: datetime | None = None,
        date_finish: datetime | None = None,
    ) -> list[dict[str, Any]]:
        _ = (date_start, date_finish)
        response = self._request_json(
            method="GET",
            path=self._build_path("/api/seller-last-sales"),
            params={**self._auth_query_params(), "group": "false", "top": "1000"},
            headers={"Accept": "application/json"},
        )
        if isinstance(response, list):
            return [self._normalize_sales_item(item) for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            self._ensure_success_response(response, context="GGSEL latest sales failed")
            return [self._normalize_sales_item(item) for item in self._extract_sales_items(response)]
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

    def _send_buyer_message_with_token(
        self,
        *,
        order_id: str,
        buyer_id: str,
        message: str,
        token: str,
    ) -> dict[str, Any]:
        _ = buyer_id
        path = self._build_path("/api/debates/v2")
        try:
            response = self.http.request(
                method="POST",
                url=path,
                params={"token": token, "id_i": order_id},
                json={"message": message, "files": []},
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise APIRequestError("GGSEL request failed: POST /api/debates/v2") from exc

        if response.is_error:
            detail = self._response_error_excerpt(response)
            suffix = f": {detail}" if detail else ""
            if response.status_code in {401, 403}:
                suffix = f"{suffix} ({self._auth_error_hint()})"
            raise APIRequestError(
                f"GGSEL returned HTTP {response.status_code} for POST {path}{suffix}"
            )

        if not response.content:
            return {"status": "ok"}
        try:
            parsed = response.json()
        except ValueError:
            return {"status": "ok", "response": response.text.strip()}
        if isinstance(parsed, dict):
            self._ensure_success_response(parsed, context="GGSEL buyer message failed")
            parsed.setdefault("status", "ok")
            return parsed
        return {"status": "ok", "response": parsed}

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        last_auth_error: APIRequestError | None = None
        refresh_rounds = (False, True) if self.seller_id else (False,)
        for force_refresh in refresh_rounds:
            token = self._get_access_token(force_refresh=force_refresh)
            try:
                return self._send_buyer_message_with_token(
                    order_id=order_id,
                    buyer_id=buyer_id,
                    message=message,
                    token=token,
                )
            except APIRequestError as exc:
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    last_auth_error = exc
                    continue
                raise

        if last_auth_error is not None:
            raise last_auth_error
        return self._send_buyer_message_with_token(
            order_id=order_id,
            buyer_id=buyer_id,
            message=message,
            token=self._get_access_token(),
        )
