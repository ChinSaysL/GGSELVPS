from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.clients.base import APIClientConfigError, APIRequestError, BaseAPIClient


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

    _TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
    _TASK_POLL_INTERVAL_SECONDS = 0.5
    _TASK_POLL_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        seller_id: str | int | None = None,
        task_poll_timeout_seconds: float | None = None,
        task_poll_interval_seconds: float | None = None,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(
            service_name="Digiseller",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        self.seller_id = str(seller_id).strip() if seller_id is not None else None
        if self.seller_id == "":
            self.seller_id = None
        self._access_token: str | None = None
        self._access_token_valid_thru: datetime | None = None
        self._last_apilogin_error: str | None = None
        self._last_apilogin_timestamp: int | None = None
        self._task_poll_timeout_seconds = (
            float(task_poll_timeout_seconds)
            if task_poll_timeout_seconds is not None
            else self._TASK_POLL_TIMEOUT_SECONDS
        )
        self._task_poll_interval_seconds = (
            float(task_poll_interval_seconds)
            if task_poll_interval_seconds is not None
            else self._TASK_POLL_INTERVAL_SECONDS
        )

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
                "DIGISELLER_SELLER_ID is required to auto-refresh Digiseller access tokens."
            )
        try:
            seller_id = int(self.seller_id)
        except ValueError as exc:
            raise APIClientConfigError("DIGISELLER_SELLER_ID must be an integer.") from exc

        api_key = self._require_api_key()
        timestamp = self._next_apilogin_timestamp()
        sign = hashlib.sha256(f"{api_key}{timestamp}".encode("utf-8")).hexdigest()
        response = self._request_json(
            method="POST",
            path="/api/apilogin",
            json_body={
                "seller_id": seller_id,
                "timestamp": timestamp,
                "sign": sign,
            },
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller /api/apilogin returned unexpected response format.")

        retval = response.get("retval")
        token_raw = response.get("token")
        token = token_raw.strip() if isinstance(token_raw, str) else ""
        if retval not in (0, "0") or not token:
            desc = str(response.get("desc") or "").strip() or "unknown error"
            raise APIRequestError(f"Digiseller /api/apilogin failed: {desc}")

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
            # Fallback for environments that still provide a direct short-lived token
            # in DIGISELLER_API_KEY instead of seller API key + seller ID.
            self._last_apilogin_error = str(exc)
            return api_key

        self._access_token = token
        self._access_token_valid_thru = valid_thru
        self._last_apilogin_error = None
        return token

    def _auth_query_params(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = self._get_access_token(force_refresh=force_refresh)
        return {"token": token}

    def _auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = self._get_access_token(force_refresh=force_refresh)
        return {
            "token": token,
            "Authorization": f"Bearer {token}",
        }

    @staticmethod
    def _collect_offer_dicts(value: object) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                collected.extend(DigisellerClient._collect_offer_dicts(item))
            return collected
        if not isinstance(value, dict):
            return collected

        if DigisellerClient._extract_offer_id(value) is not None:
            collected.append(value)

        for nested in value.values():
            collected.extend(DigisellerClient._collect_offer_dicts(nested))
        return collected

    @staticmethod
    def _extract_offer_id(item: dict[str, Any]) -> str | None:
        for key in ("id", "product_id", "productId", "goods_id", "good_id"):
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def list_offers(self) -> list[dict[str, Any]]:
        """
        Best-effort listing from /api/products/list.

        Digiseller docs are inconsistent across accounts for accepted body shape, so we
        try several common payload variants and normalize whatever product-like dicts
        come back. Duplicates are de-duplicated by offer id.
        """
        attempts: tuple[dict[str, Any], ...] = (
            {},
            {"ids": []},
            {"product_ids": []},
            {"products": []},
            {"page": 1, "per_page": 1000},
            {"offset": 0, "limit": 1000},
        )
        merged: dict[str, dict[str, Any]] = {}

        for body in attempts:
            response: dict[str, Any] | list[Any] | None = None
            # Try strict swagger query-token auth first.
            try:
                response = self._request_json(
                    method="POST",
                    path="/api/products/list",
                    params=self._auth_query_params(),
                    json_body=body,
                )
            except Exception:
                response = None

            # Then try header-based auth for accounts where query-token is rejected.
            if response is None:
                try:
                    response = self._request_json(
                        method="POST",
                        path="/api/products/list",
                        json_body=body,
                        headers=self._auth_headers(),
                    )
                except Exception:
                    response = None

            if response is None:
                continue

            items = self._collect_offer_dicts(response)
            for item in items:
                offer_id = self._extract_offer_id(item)
                if offer_id is None:
                    continue
                merged.setdefault(offer_id, item)

            if merged:
                # Stop after first successful non-empty response to reduce API load.
                break

        return list(merged.values())

    @staticmethod
    def _matches_offer_id(item: dict[str, Any], offer_id: str) -> bool:
        candidates = (
            "id",
            "product_id",
            "productId",
            "goods_id",
            "good_id",
        )
        normalized_offer_id = offer_id.strip()
        for key in candidates:
            value = item.get(key)
            if value is None:
                continue
            if str(value).strip() == normalized_offer_id:
                return True
        return False

    @staticmethod
    def _extract_offer(item: dict[str, Any], offer_id: str) -> dict[str, Any] | None:
        if DigisellerClient._matches_offer_id(item, offer_id):
            return item

        for value in item.values():
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, dict):
                        matched = DigisellerClient._extract_offer(child, offer_id)
                        if matched is not None:
                            return matched
            if isinstance(value, dict):
                matched = DigisellerClient._extract_offer(value, offer_id)
                if matched is not None:
                    return matched
        return None

    def get_offer_by_id(self, offer_id: str) -> dict[str, Any] | None:
        normalized = offer_id.strip()
        if not normalized:
            return None

        api_offer_id: int | str
        try:
            api_offer_id = int(normalized)
        except ValueError:
            api_offer_id = normalized

        attempts: list[
            tuple[
                str,
                str,
                dict[str, Any] | None,
                dict[str, str] | None,
                dict[str, Any] | None,
            ]
        ] = [
            # Public product info endpoint from swagger docs.
            ("GET", f"/api/products/{normalized}/data", None, {"Accept": "application/json"}, None),
            (
                "GET",
                f"/api/products/{normalized}/data",
                None,
                {"Accept": "application/json", **self._auth_headers()},
                None,
            ),
            ("POST", "/api/products/list", {"ids": [api_offer_id]}, self._auth_headers(), None),
            ("POST", "/api/products/list", {"product_ids": [api_offer_id]}, self._auth_headers(), None),
            ("POST", "/api/products/list", {"products": [api_offer_id]}, self._auth_headers(), None),
            ("GET", f"/api/products/{normalized}", None, self._auth_headers(), None),
            ("GET", f"/api/product/{normalized}", None, self._auth_headers(), None),
        ]

        for method, path, body, headers, params in attempts:
            try:
                response = self._request_json(
                    method=method,
                    path=path,
                    params=params,
                    json_body=body,
                    headers=headers,
                )
            except Exception:
                continue

            if isinstance(response, dict):
                product_obj = response.get("product")
                if isinstance(product_obj, dict):
                    product_obj.setdefault("product_id", product_obj.get("id"))
                    return product_obj
                extracted = self._extract_offer(response, normalized)
                if extracted is not None:
                    return extracted
                if self._matches_offer_id(response, normalized):
                    return response

            if isinstance(response, list):
                for item in response:
                    if isinstance(item, dict) and self._matches_offer_id(item, normalized):
                        return item

        return None

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
        token = self._get_access_token()
        response = self._request_json(
            method="POST",
            path="/api/product/edit/V2/status",
            params={"token": token},
            json_body={
                "new_status": "enabled" if enabled else "disabled",
                "products": [int(offer_id)],
            },
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller status update returned unexpected response format.")
        self._ensure_success_response(response, context="Digiseller status update failed")

        task_id = self._extract_task_id(response)
        if task_id is None:
            content = response.get("content")
            if isinstance(content, str) and content.strip():
                task_id = content.strip()

        if task_id:
            task_status = self._wait_for_price_update_task(task_id=task_id, token=token)
            task_status.setdefault("task_id", task_id)
            return task_status
        return {"status": "ok"}

    def _request_price_update(
        self,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        request_payload: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            response = self.http.request(
                method="POST",
                url="/api/product/edit/prices",
                params=params,
                json=request_payload,
                headers=headers,
            )
        except httpx.RequestError as exc:
            raise APIRequestError("Digiseller request failed: POST /api/product/edit/prices") from exc

        if response.is_error:
            detail = self._response_error_excerpt(response)
            suffix = f": {detail}" if detail else ""
            raise APIRequestError(
                "Digiseller returned HTTP "
                f"{response.status_code} for POST /api/product/edit/prices{suffix}"
            )

        try:
            parsed = response.json()
        except ValueError:
            # Digiseller can return a plain-text async task id for this endpoint.
            task_id = response.text.strip()
            if not task_id:
                return {"status": "ok"}
            if task_id.startswith("<") and "html" in task_id.lower():
                raise APIRequestError(
                    "Digiseller returned unexpected HTML for POST /api/product/edit/prices"
                )
            return {"status": "ok", "task_id": task_id}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            token = parsed.strip()
            if token:
                return {"status": "ok", "task_id": token}
            return {"status": "ok"}
        if isinstance(parsed, list):
            return {"status": "ok", "items": parsed}
        return {"status": "ok", "response": parsed}

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
            normalized = text.lower()
            if normalized == "done":
                return 3
            if normalized == "error":
                return 2
            if normalized == "inprogress":
                return 1
            if normalized == "inqueue":
                return 0
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
        fallback_errors = {
            str(k).strip(): str(v).strip() 
            for k, v in task_status.items() 
            if str(k).lower() not in ("status", "task_id") and v not in (None, "")
        }
        if fallback_errors:
            return repr(fallback_errors)

        return ""
        return str(raw_errors).strip()

    def _fetch_price_update_task_status(self, *, task_id: str, token: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path="/api/product/edit/UpdateProductsTaskStatus",
            params={"taskId": task_id, "token": token},
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller task status returned unexpected response format.")
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
                    error_text = self._task_error_descriptions(status_payload)
                    if not error_text:
                        error_text = "unknown task failure"
                    raise APIRequestError(
                        f"Digiseller price update task completed with errors ({task_id}): {error_text}"
                    )
                success_payload = dict(status_payload)
                success_payload.setdefault("status", "ok")
                success_payload.setdefault("task_id", task_id)
                return success_payload
            if status_code == 2:
                error_text = self._task_error_descriptions(status_payload)
                if not error_text:
                    error_text = "unknown task failure"
                raise APIRequestError(
                    f"Digiseller price update task failed ({task_id}): {error_text}"
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
                "Digiseller price update task timed out "
                f"({task_id}), last status={status_code}"
            )
        raise APIRequestError(f"Digiseller price update task timed out ({task_id}).")

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
        for key in ("id", "option_id", "optionId"):
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
        for key in ("variant_id", "variantId", "id"):
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

    def _list_product_option_ids(self, product_id: str, *, token: str) -> list[int]:
        response = self._request_json(
            method="GET",
            path=f"/api/products/options/list/{product_id}",
            params={"token": token},
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller options list returned unexpected response format.")
        self._ensure_success_response(response, context="Digiseller options/list failed")

        content = response.get("content")
        if not isinstance(content, list):
            return []
        option_ids: list[int] = []
        for item in content:
            option_id = self._extract_option_id(item)
            if option_id is None:
                continue
            option_ids.append(option_id)
        return option_ids

    def _get_product_option(self, option_id: int, *, token: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=f"/api/products/options/{option_id}",
            params={"token": token},
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller option details returned unexpected response format.")
        self._ensure_success_response(response, context="Digiseller options/details failed")

        content = response.get("content")
        if not isinstance(content, dict):
            raise APIRequestError("Digiseller option details missing content object.")
        return content

    def _find_variant_payload(
        self,
        *,
        product_id: str,
        variant_id: int,
        token: str,
    ) -> tuple[int, dict[str, Any]]:
        option_ids = self._list_product_option_ids(product_id, token=token)
        for option_id in option_ids:
            option = self._get_product_option(option_id, token=token)
            variants = option.get("variants")
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                if self._extract_variant_id(variant) == variant_id:
                    return option_id, variant
        raise APIRequestError(
            f"Digiseller variant {variant_id} was not found on product {product_id}."
        )

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

        payload: dict[str, Any] = {
            "name": variant.get("name"),
            "type": variant.get("type", "priceplus"),
            "rate": variant.get("rate", 0),
            "default": target_default,
            "visible": bool(visible),
            "order": int(variant.get("order", 1)),
        }
        response = self._request_json(
            method="POST",
            path=f"/api/products/options/{option_id}/variants/{variant_id}",
            params={"token": token},
            json_body=payload,
        )
        if not isinstance(response, dict):
            raise APIRequestError("Digiseller variant update returned unexpected response format.")
        self._ensure_success_response(response, context="Digiseller variant visibility update failed")
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
            query_auth = {"token": token}
            header_auth = {"token": token, "Authorization": f"Bearer {token}"}
            attempts: tuple[tuple[dict[str, Any] | None, dict[str, str] | None], ...] = (
                (query_auth, None),
                (None, header_auth),
                (query_auth, header_auth),
            )

            for params, headers in attempts:
                try:
                    response = self._request_price_update(
                        params=params,
                        headers=headers,
                        request_payload=request_payload,
                    )
                except APIRequestError as exc:
                    # Some Digiseller accounts reject one auth style for this endpoint.
                    # Also retry once with a refreshed token when seller_id is configured.
                    error_text = str(exc)
                    if "HTTP 401" in error_text or "HTTP 403" in error_text:
                        last_auth_error = exc
                        continue
                    # Header-only attempts can return 400 when this endpoint requires
                    # query-token auth. Keep trying the remaining auth combinations.
                    if "HTTP 400" in error_text and params is None and headers is not None:
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
            details = (
                "Digiseller authorization failed for live price update. "
                "Ensure token permission includes [Products]: Edit."
            )
            if self.seller_id:
                if self._last_apilogin_error:
                    details = (
                        f"{details} Also, /api/apilogin failed with: {self._last_apilogin_error}. "
                        "Check DIGISELLER_API_KEY (seller API key) and DIGISELLER_SELLER_ID."
                    )
            else:
                details = (
                    f"{details} Digiseller access tokens are short-lived. "
                    "Set DIGISELLER_SELLER_ID to auto-refresh token via /api/apilogin."
                )
            raise APIRequestError(f"{last_auth_error}. {details}")
        raise APIRequestError("Digiseller price update failed before receiving a response.")

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

    def verify_unique_code(self, unique_code: str) -> dict[str, Any]:
        normalized_code = unique_code.strip()
        if not normalized_code:
            raise APIRequestError("Digiseller unique code is empty.")
        response = self._request_json(
            method="GET",
            path=f"/api/purchases/unique-code/{normalized_code}",
            params=self._auth_query_params(),
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller unique-code response format.")
        self._ensure_success_response(response, context="Digiseller unique-code verification failed")
        return response

    def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]:
        normalized_code = unique_code.strip()
        if not normalized_code:
            raise APIRequestError("Digiseller unique code is empty.")
        response = self._request_json(
            method="PUT",
            path=f"/api/purchases/unique-code/{normalized_code}/deliver",
            params=self._auth_query_params(),
            headers={"Accept": "application/json"},
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected Digiseller unique-code delivery response format.")
        retval = response.get("retval")
        if retval in (0, "0", 4, "4"):
            return response
        self._ensure_success_response(
            response,
            context="Digiseller unique-code delivery confirmation failed",
        )
        return response

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        _ = buyer_id
        token = self._get_access_token()
        try:
            response = self.http.request(
                method="POST",
                url="/api/debates/v2",
                params={"token": token, "id_i": order_id},
                json={"message": message, "files": []},
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise APIRequestError("Digiseller request failed: POST /api/debates/v2") from exc

        if response.is_error:
            detail = self._response_error_excerpt(response)
            suffix = f": {detail}" if detail else ""
            raise APIRequestError(
                f"Digiseller returned HTTP {response.status_code} for POST /api/debates/v2{suffix}"
            )

        if not response.content:
            return {"status": "ok"}
        try:
            parsed = response.json()
        except ValueError:
            return {"status": "ok", "response": response.text.strip()}
        if isinstance(parsed, dict):
            self._ensure_success_response(parsed, context="Digiseller buyer message failed")
            parsed.setdefault("status", "ok")
            return parsed
        return {"status": "ok", "response": parsed}
