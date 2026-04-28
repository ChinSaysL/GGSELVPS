from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
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
    - GET /v2.1/recharge/services
    - POST /v2.1/recharge/orders/{service_id}
    - POST /v2.1/recharge/orders/{order_id}/pay
    - GET /v2.1/recharge/orders/{order_id}
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

    @staticmethod
    def _first_non_empty_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
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

    def _extract_paginated_items(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("items", "services", "rows", "results", "data"):
            items = self._extract_dict_items(response.get(key))
            if items:
                return items

        data = response.get("data")
        if isinstance(data, dict):
            for key in ("items", "services", "rows", "results"):
                items = self._extract_dict_items(data.get(key))
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

    def list_shop_products(self) -> list[dict[str, Any]]:
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

    def list_products(self) -> list[dict[str, Any]]:
        products = self.list_shop_products()
        try:
            products.extend(self.list_recharge_products())
        except APIClientError:
            # Not every G-Engine account has access to the recharge catalogue. Keep
            # shop gift-card import working and let explicit recharge imports show
            # the concrete API error to the operator.
            pass
        return products

    def list_recharge_services(
        self,
        *,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        services: list[dict[str, Any]] = []
        offset = 0
        page_limit = max(1, min(limit, 100))

        seen_page_signatures: set[tuple[str, ...]] = set()
        max_pages = 200

        while len(seen_page_signatures) < max_pages:
            params: dict[str, Any] = {
                "limit": page_limit,
                "offset": offset,
            }
            if search:
                params["search"] = search

            response = self._request_json(
                method="GET",
                path=self._build_path("/recharge/services"),
                headers=self._auth_headers(),
                params=params,
            )
            if not isinstance(response, dict):
                raise TypeError("Unexpected G-Engine recharge services response format.")

            items = self._extract_paginated_items(response)
            page_signature = tuple(str(item.get("id") or "") for item in items)
            if page_signature in seen_page_signatures:
                break
            seen_page_signatures.add(page_signature)
            services.extend(items)

            total = response.get("total")
            if isinstance(response.get("data"), dict):
                total = response["data"].get("total", total)
            try:
                total_count = int(str(total)) if total is not None else None
            except ValueError:
                total_count = None

            if not items:
                break
            offset += len(items)
            if total_count is not None and offset >= total_count:
                break

        return services

    def list_recharge_products(self) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        for service in self.list_recharge_services():
            products.extend(self._expand_recharge_service(service, include_unpriced=False))
        return products

    def list_recharge_service_products(
        self,
        service_id: str,
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
        include_unpriced: bool = True,
    ) -> list[dict[str, Any]]:
        service = self._find_recharge_service(
            service_id,
            search=source_slug or fallback_title,
        )
        if service is None:
            service = self._fallback_recharge_service_from_link(
                service_id,
                fallback_title=fallback_title,
                source_slug=source_slug,
            )
        if service is None:
            return []
        return self._expand_recharge_service(
            service,
            fallback_title=fallback_title,
            source_slug=source_slug,
            include_unpriced=include_unpriced,
        )

    @staticmethod
    def _fallback_recharge_service_from_link(
        service_id: str,
        *,
        fallback_title: str | None,
        source_slug: str | None,
    ) -> dict[str, Any] | None:
        normalized_slug = (source_slug or "").strip().lower()
        if str(service_id).strip() != "1" or "steam" not in normalized_slug:
            return None
        return {
            "id": "1",
            "name": fallback_title or "Steam",
            "description": "Imported from G-Engine recharge link fallback.",
            "type": "unfixed",
            "params": [
                {"param_key": "Account", "param_type": "String"},
                {"param_key": "Quantity", "param_type": "Float"},
            ],
            "denominations": None,
            "unfixed_details": None,
            "import_note": "fallback_from_recharge_link",
        }

    def _find_recharge_service(
        self,
        service_id: str,
        *,
        search: str | None,
    ) -> dict[str, Any] | None:
        normalized_service_id = str(service_id).strip()
        search_terms = [term for term in (search, normalized_service_id) if term]
        seen_terms: set[str] = set()
        for term in search_terms:
            if term in seen_terms:
                continue
            seen_terms.add(term)
            for service in self.list_recharge_services(search=term):
                if str(service.get("id") or "").strip() == normalized_service_id:
                    return service

        for service in self.list_recharge_services():
            if str(service.get("id") or "").strip() == normalized_service_id:
                return service
        return None

    def _expand_recharge_service(
        self,
        service: dict[str, Any],
        *,
        fallback_title: str | None = None,
        source_slug: str | None = None,
        include_unpriced: bool,
    ) -> list[dict[str, Any]]:
        service_id = self._first_non_empty_str(service, ("id", "service_id"))
        if service_id is None:
            return []

        denominations = self._extract_dict_items(service.get("denominations"))
        if denominations:
            products: list[dict[str, Any]] = []
            for denomination in denominations:
                product = self._recharge_denomination_product(
                    service,
                    denomination,
                    service_id=service_id,
                    fallback_title=fallback_title,
                    source_slug=source_slug,
                )
                if product is not None:
                    products.append(product)
            return products

        product = self._recharge_unfixed_product(
            service,
            service_id=service_id,
            fallback_title=fallback_title,
            source_slug=source_slug,
            include_unpriced=include_unpriced,
        )
        return [] if product is None else [product]

    def _recharge_base_product(
        self,
        service: dict[str, Any],
        *,
        service_id: str,
        fallback_title: str | None,
        source_slug: str | None,
    ) -> dict[str, Any]:
        title = (
            self._first_non_empty_str(service, ("name", "title", "product_name"))
            or fallback_title
            or f"Recharge service {service_id}"
        )
        params = self._extract_dict_items(service.get("params"))
        payload = dict(service)
        payload.update(
            {
                "source_type": "recharge",
                "recharge_product_id": service_id,
                "service_id": service_id,
                "service_name": title,
                "service_type": service.get("type"),
                "source_slug": source_slug,
                "title": title,
                "name": title,
                "category": "Recharge",
                "recharge_param_keys": [
                    str(param.get("param_key"))
                    for param in params
                    if param.get("param_key") is not None
                ],
            }
        )
        return payload

    def _recharge_denomination_product(
        self,
        service: dict[str, Any],
        denomination: dict[str, Any],
        *,
        service_id: str,
        fallback_title: str | None,
        source_slug: str | None,
    ) -> dict[str, Any] | None:
        denomination_id = self._first_non_empty_str(denomination, ("id", "denomination_id"))
        price = self._first_non_empty_str(denomination, ("cost_usd", "cost", "price_usd", "price"))
        if denomination_id is None or price is None:
            return None

        product = self._recharge_base_product(
            service,
            service_id=service_id,
            fallback_title=fallback_title,
            source_slug=source_slug,
        )
        product.update(denomination)
        denomination_name = (
            self._first_non_empty_str(denomination, ("name", "title", "denomination"))
            or self._recharge_value_label(denomination)
            or f"Denomination {denomination_id}"
        )
        product.update(
            {
                "id": f"recharge:{service_id}:{denomination_id}",
                "recharge_product_id": service_id,
                "recharge_denomination_id": str(denomination_id),
                "denomination_id": str(denomination_id),
                "denomination": denomination_name,
                "cost_usd": price,
                "stock": self._first_non_empty_str(denomination, ("stock", "quantity")) or "unlimited",
                "is_available": True,
            }
        )
        return product

    def _recharge_unfixed_product(
        self,
        service: dict[str, Any],
        *,
        service_id: str,
        fallback_title: str | None,
        source_slug: str | None,
        include_unpriced: bool,
    ) -> dict[str, Any] | None:
        product = self._recharge_base_product(
            service,
            service_id=service_id,
            fallback_title=fallback_title,
            source_slug=source_slug,
        )
        details = service.get("unfixed_details")
        details_payload = details if isinstance(details, dict) else {}
        price = (
            self._first_non_empty_str(service, ("cost_usd", "cost", "price_usd", "price", "rate"))
            or self._first_non_empty_str(details_payload, ("cost_usd", "cost", "price_usd", "price", "rate"))
        )
        if price is None and not include_unpriced:
            return None

        currency = self._first_non_empty_str(details_payload, ("currency", "in_game_currency"))
        denomination = f"Variable {currency}" if currency else "Variable amount"
        product.update(
            {
                "id": f"recharge:{service_id}",
                "denomination": denomination,
                "cost_usd": price or "0",
                "stock": "unlimited" if price is not None else "0",
                "is_available": price is not None,
                "requires_manual_cost": price is None,
            }
        )
        return product

    @staticmethod
    def _recharge_value_label(payload: dict[str, Any]) -> str | None:
        value = payload.get("value")
        currency = payload.get("currency")
        if value in {None, ""}:
            return None
        if currency in {None, ""}:
            return str(value).strip()
        return f"{str(value).strip()} {str(currency).strip()}"

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

    @staticmethod
    def _coerce_item_id(product_id: str) -> int | str:
        try:
            return int(product_id)
        except ValueError:
            return product_id

    @staticmethod
    def _clean_buyer_inputs(buyer_inputs: dict[str, str] | None) -> dict[str, str]:
        if not buyer_inputs:
            return {}
        cleaned: dict[str, str] = {}
        for key, value in buyer_inputs.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if normalized_key and normalized_value:
                cleaned[normalized_key] = normalized_value
        return cleaned

    @staticmethod
    def _is_recharge_product_id(product_id: str) -> bool:
        return str(product_id).startswith("recharge:")

    @staticmethod
    def _parse_recharge_product_id(product_id: str) -> tuple[str, str | None]:
        parts = str(product_id).split(":")
        if len(parts) not in {2, 3} or parts[0] != "recharge" or not parts[1]:
            raise APIClientError(f"Invalid G-Engine recharge supplier product ID: {product_id}")
        return parts[1], parts[2] if len(parts) == 3 and parts[2] else None

    @staticmethod
    def _normalize_param_key(value: object) -> str:
        return "".join(character.lower() for character in str(value) if character.isalnum())

    @classmethod
    def _payload_param_keys(cls, product_payload: dict[str, Any]) -> list[str]:
        raw_keys = product_payload.get("recharge_param_keys")
        if isinstance(raw_keys, list):
            keys = [str(item).strip() for item in raw_keys if str(item).strip()]
            if keys:
                return keys

        raw_params = product_payload.get("params")
        if isinstance(raw_params, list):
            keys = []
            for item in raw_params:
                if not isinstance(item, dict):
                    continue
                value = item.get("param_key")
                if value is not None and str(value).strip():
                    keys.append(str(value).strip())
            if keys:
                return keys
        return ["Account"]

    @classmethod
    def _value_for_recharge_param(
        cls,
        param_key: str,
        *,
        buyer_inputs: dict[str, str],
        default_params: dict[str, Any],
    ) -> str | None:
        normalized_param = cls._normalize_param_key(param_key)
        for key, value in default_params.items():
            if cls._normalize_param_key(key) == normalized_param and value not in {None, ""}:
                return str(value).strip()

        for key, value in buyer_inputs.items():
            if cls._normalize_param_key(key) == normalized_param and value:
                return value

        if normalized_param == "account" and buyer_inputs:
            for key, value in buyer_inputs.items():
                normalized_key = cls._normalize_param_key(key)
                if normalized_key in {
                    "quantity",
                    "currency",
                    "amount",
                    "sum",
                    "option",
                    "variant",
                    "denomination",
                }:
                    continue
                return value
        return None

    @staticmethod
    def _decimal_payload_value(value: object, *, default: Decimal) -> Decimal:
        if value in {None, ""}:
            return default
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return default

    @staticmethod
    def _format_decimal_param(value: Decimal) -> str:
        normalized = value.normalize()
        return format(normalized, "f")

    @classmethod
    def _recharge_default_params(
        cls,
        *,
        product_payload: dict[str, Any],
        marketplace_quantity: int,
    ) -> dict[str, Any]:
        default_params_payload = product_payload.get("recharge_default_params")
        default_params = dict(default_params_payload) if isinstance(default_params_payload, dict) else {}

        quantity_source = str(product_payload.get("recharge_quantity_source") or "").strip().lower()
        if quantity_source == "marketplace_quantity":
            multiplier = cls._decimal_payload_value(
                product_payload.get("recharge_quantity_multiplier"),
                default=Decimal("1"),
            )
            recharge_quantity = Decimal(marketplace_quantity) * multiplier
            default_params["Quantity"] = cls._format_decimal_param(recharge_quantity)
        return default_params

    @classmethod
    def _build_recharge_params(
        cls,
        *,
        product_payload: dict[str, Any],
        buyer_inputs: dict[str, str],
        marketplace_quantity: int,
    ) -> list[dict[str, str]]:
        default_params = cls._recharge_default_params(
            product_payload=product_payload,
            marketplace_quantity=marketplace_quantity,
        )
        params: list[dict[str, str]] = []
        missing: list[str] = []

        for param_key in cls._payload_param_keys(product_payload):
            value = cls._value_for_recharge_param(
                param_key,
                buyer_inputs=buyer_inputs,
                default_params=default_params,
            )
            if value is None:
                missing.append(param_key)
                continue
            params.append({"param_key": param_key, "param_value": value})

        if missing:
            raise APIClientError(
                "Missing buyer/default input for G-Engine recharge parameter(s): "
                f"{', '.join(missing)}"
            )
        return params

    @staticmethod
    def _recharge_uuid(external_order_id: str | None) -> str | None:
        if not external_order_id:
            return None
        return uuid.uuid5(uuid.NAMESPACE_URL, external_order_id).hex

    @staticmethod
    def _extract_recharge_order_id(payload: dict[str, Any]) -> str | None:
        for key in ("id", "order_id", "orderId", "supplier_order_id"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                text = str(value).strip()
                return text.split(":", maxsplit=1)[1] if text.startswith("recharge:") else text
        return None

    @classmethod
    def _mark_recharge_order_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = cls._extract_recharge_order_id(payload)
        if order_id:
            payload = dict(payload)
            payload["supplier_order_id"] = f"recharge:{order_id}"
        return payload

    def _pay_recharge_order(self, order_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path=self._build_path(f"/recharge/orders/{order_id}/pay"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine recharge pay response format.")
        return self._mark_recharge_order_payload(response)

    def _create_recharge_order(
        self,
        product_id: str,
        quantity: int,
        *,
        buyer_inputs: dict[str, str] | None,
        external_order_id: str | None,
        product_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = product_payload if isinstance(product_payload, dict) else {}
        quantity_source = str(payload.get("recharge_quantity_source") or "").strip().lower()
        if quantity != 1 and quantity_source != "marketplace_quantity":
            raise APIClientError(
                "G-Engine recharge fulfillment with marketplace quantity > 1 requires "
                "recharge_quantity_source=marketplace_quantity."
            )

        service_id, denomination_id = self._parse_recharge_product_id(product_id)
        denomination_id = str(payload.get("recharge_denomination_id") or denomination_id or "")

        order_payload: dict[str, Any] = {
            "params": self._build_recharge_params(
                product_payload=payload,
                buyer_inputs=self._clean_buyer_inputs(buyer_inputs),
                marketplace_quantity=quantity,
            )
        }
        if denomination_id:
            order_payload["denomination_id"] = self._coerce_item_id(denomination_id)
        recharge_uuid = self._recharge_uuid(external_order_id)
        if recharge_uuid:
            order_payload["uuid"] = recharge_uuid

        response = self._request_json(
            method="POST",
            path=self._build_path(f"/recharge/orders/{service_id}"),
            headers=self._auth_headers(),
            json_body=order_payload,
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine recharge order response format.")

        marked_response = self._mark_recharge_order_payload(response)
        order_id = self._extract_recharge_order_id(marked_response)
        status = str(marked_response.get("status") or "").strip().lower()
        if order_id and status == "verified":
            return self._pay_recharge_order(order_id)
        return marked_response

    def create_order(
        self,
        product_id: str,
        quantity: int = 1,
        *,
        buyer_inputs: dict[str, str] | None = None,
        external_order_id: str | None = None,
        product_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._is_recharge_product_id(product_id):
            return self._create_recharge_order(
                product_id,
                quantity,
                buyer_inputs=buyer_inputs,
                external_order_id=external_order_id,
                product_payload=product_payload,
            )

        item: dict[str, Any] = {
            "id": self._coerce_item_id(product_id),
            "quantity": quantity,
        }
        cleaned_inputs = self._clean_buyer_inputs(buyer_inputs)
        if cleaned_inputs:
            item["fields"] = cleaned_inputs
            first_value = next(iter(cleaned_inputs.values()))
            item["account"] = first_value

        order_payload: dict[str, Any] = {
            "items": [
                item
            ]
        }
        if external_order_id:
            order_payload["external_id"] = external_order_id

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
        if self._is_recharge_product_id(order_id):
            recharge_order_id = str(order_id).split(":", maxsplit=1)[1]
            response = self._request_json(
                method="GET",
                path=self._build_path(f"/recharge/orders/{recharge_order_id}"),
                headers=self._auth_headers(),
            )
            if not isinstance(response, dict):
                raise TypeError("Unexpected G-Engine recharge order status response format.")
            marked_response = self._mark_recharge_order_payload(response)
            status = str(marked_response.get("status") or "").strip().lower()
            if status == "verified":
                return self._pay_recharge_order(recharge_order_id)
            return marked_response

        response = self._request_json(
            method="GET",
            path=self._build_path(f"/shop/orders/{order_id}"),
            headers=self._auth_headers(),
        )
        if not isinstance(response, dict):
            raise TypeError("Unexpected G-Engine order status response format.")
        return response
