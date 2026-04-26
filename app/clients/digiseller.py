from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

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

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        seller_id: str | int | None = None,
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
        _ = (offer_id, enabled)
        # TODO: Availability flag field name is not confirmed in the docs used here.
        raise NotImplementedError(
            "Digiseller set_offer_enabled is not implemented. "
            "Please provide the exact payload field for enable/disable."
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
                    response = self._request_json(
                        method="POST",
                        path="/api/product/edit/prices",
                        params=params,
                        json_body=request_payload,
                        headers=headers,
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

                if not isinstance(response, dict):
                    raise TypeError("Unexpected Digiseller price update response format.")
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

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]:
        _ = (order_id, buyer_id, message)
        # TODO: /api/debates/v2 requires correspondence ID (id_i). Mapping from
        # marketplace order to correspondence ID is not defined yet.
        raise NotImplementedError(
            "Digiseller send_buyer_message is not implemented. "
            "Please provide how to resolve correspondence ID from order data."
        )
