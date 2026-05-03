from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.clients import DigisellerClient, GEngineClient, GGSELClient
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import MarketplaceOffer, Order, ProductMapping, SyncRun
from app.services.delivery import (
    DeliveryProductKind,
    build_delivery_message,
    build_provider_unavailable_message,
    build_purchase_message,
    build_unique_code_request_message,
    detect_delivery_product_kind,
    masked_code_for_logs,
)
from app.services.provider_status import (
    SUPPLIER_PROVIDER_UNAVAILABLE,
    SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED,
    is_temporary_provider_error,
)
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

OrderStatus = Literal[
    "pending",
    "processing",
    "supplier_order_created",
    "delivered",
    "failed",
    "needs_manual_review",
]

RetryAction = Literal["none", "retry_processing", "retry_delivery"]


class MarketplaceOrderClientProtocol(Protocol):
    def get_latest_sales(
        self,
        *,
        date_start: datetime | None = None,
        date_finish: datetime | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_order(self, order_id: str) -> dict[str, Any]: ...

    def send_buyer_message(self, order_id: str, buyer_id: str, message: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


class DigisellerDeliveryClientProtocol(MarketplaceOrderClientProtocol, Protocol):
    def verify_unique_code(self, unique_code: str) -> dict[str, Any]: ...

    def mark_unique_code_delivered(self, unique_code: str) -> dict[str, Any]: ...

    def get_buyer_messages(self, order_id: str) -> list[dict[str, Any]]: ...


class SupplierOrderClientProtocol(Protocol):
    def create_order(
        self,
        product_id: str,
        quantity: int = 1,
        *,
        buyer_inputs: dict[str, str] | None = None,
        external_order_id: str | None = None,
        product_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_order_status(self, order_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class NormalizedMarketplaceSale:
    marketplace: str
    order_id: str | None
    offer_id: str | None
    variant_id: int | None
    variant_text: str | None
    buyer_id: str | None
    sale_price: Decimal | None
    quantity: int
    paid: bool
    unique_code: str | None
    buyer_inputs: dict[str, str]
    raw_payload: dict[str, Any]


def should_create_supplier_purchase(existing_status: str | None) -> bool:
    if existing_status is None:
        return True
    return existing_status == "pending"


def should_skip_order(existing_status: str | None) -> bool:
    return existing_status in {"processing", "supplier_order_created", "delivered"}


def get_retry_action(status: str, supplier_order_id: str | None) -> RetryAction:
    if status not in {"failed", "needs_manual_review"}:
        return "none"
    if supplier_order_id:
        return "retry_delivery"
    return "retry_processing"


_RECREATABLE_SUPPLIER_TERMINAL_ERRORS = (
    "supplier_order_terminal:canceled",
    "supplier_order_terminal:cancelled",
    "supplier_order_terminal:expired",
)
_DIGISELLER_UNIQUE_CODE_WAIT_PREFIX = "awaiting_digiseller_unique_code"
_DIGISELLER_UNIQUE_CODE_PROMPT_SENT_MARKER = "prompt_sent"
_SUPPLIER_PURCHASE_STATUS_UNKNOWN = "supplier_purchase_status_unknown"


def _supplier_order_can_be_recreated(error_message: str | None) -> bool:
    return str(error_message or "") in _RECREATABLE_SUPPLIER_TERMINAL_ERRORS


def _sale_allows_supplier_recreate(sale: NormalizedMarketplaceSale) -> bool:
    # Automatic recreation can buy the same marketplace order twice if the
    # supplier reports a Steam gift as cancelled/expired after the first buy.
    _ = sale
    return False


def _awaiting_digiseller_unique_code(reason: str | None) -> bool:
    return str(reason or "").startswith(_DIGISELLER_UNIQUE_CODE_WAIT_PREFIX)


def _digiseller_unique_code_prompt_was_sent(reason: str | None) -> bool:
    return _DIGISELLER_UNIQUE_CODE_PROMPT_SENT_MARKER in str(reason or "").split(";")


def _mark_digiseller_unique_code_prompt_sent(reason: str | None) -> str:
    text = str(reason or _DIGISELLER_UNIQUE_CODE_WAIT_PREFIX)
    if _digiseller_unique_code_prompt_was_sent(text):
        return text
    return f"{text};{_DIGISELLER_UNIQUE_CODE_PROMPT_SENT_MARKER}"


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except ValueError:
        return None


def _first_non_empty_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _payload_content(payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content")
    if isinstance(content, dict):
        return content
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _merge_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        merged.update(payload)
        merged.update(_payload_content(payload))
    return merged


_LABEL_KEYWORD_TO_KEY: tuple[tuple[tuple[str, ...], str], ...] = (
    (("account", "login", "логин", "учетная", "аккаунт", "username", "user_name", "счёт", "счет"), "account"),
    (("quantity", "qty", "count", "amount", "sum", "колич", "сумм"), "quantity"),
    (("currency", "curr", "валют"), "currency"),
    (("region", "регион", "server", "сервер", "zone", "зона"), "region"),
)


def _normalize_input_key(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip()
    # Strip parenthetical type annotations like "(Integer)", "(String)", "(целое число)"
    raw = re.sub(r"\s*\([^)]+\)", "", raw).strip()
    lowered = raw.lower()
    for keywords, mapped_key in _LABEL_KEYWORD_TO_KEY:
        if any(kw in lowered for kw in keywords):
            return mapped_key
    text = re.sub(r"[^a-z0-9]+", "_", lowered)
    text = text.strip("_")
    return text or fallback


def _extract_numeric_quantity(value: str) -> str | None:
    compact = re.sub(r"[\s\u00A0\u202F]+", "", value)
    match = re.search(r"\d+(?:[.,]\d+)?", compact)
    if match is None:
        return None
    candidate = match.group(0).replace(",", ".")
    try:
        parsed = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    return format(parsed.normalize(), "f")


def _parse_additive_quantity(value: str | None) -> Decimal | None:
    """Extract total quantity, summing additive notation: '32 + 3 Diamonds' \u2192 Decimal('35')."""
    text = str(value or "").strip()
    if not text:
        return None
    compact = re.sub(r"[\s\u00A0\u202F]+", " ", text).strip()
    if "+" in compact:
        chain_match = re.match(r"^(\d+(?:[.,]\d+)?)(?:\s*\+\s*\d+(?:[.,]\d+)?)+", compact)
        if chain_match:
            nums = re.findall(r"\d+(?:[.,]\d+)?", chain_match.group(0))
            try:
                total = sum(Decimal(n.replace(",", ".")) for n in nums)
                if total > 0:
                    return total
            except (InvalidOperation, ValueError):
                pass
    match = re.search(r"\d+(?:[.,]\d+)?", compact)
    if match is None:
        return None
    try:
        parsed = Decimal(match.group(0).replace(",", "."))
        return parsed if parsed > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _canonical_recharge_option_key(label: str | None, fallback: str) -> str:
    raw_label = str(label or "").strip()
    lowered = raw_label.lower()
    if any(token in lowered for token in ("quantity", "qty", "count", "amount", "sum", "колич", "сумм")):
        return "quantity"
    if any(token in lowered for token in ("currency", "curr", "валют")):
        return "currency"
    return _normalize_input_key(raw_label, fallback)


def _extract_options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    options = payload.get("options")
    if isinstance(options, dict):
        option = options.get("option")
        if isinstance(option, list):
            return [item for item in option if isinstance(item, dict)]
        if isinstance(option, dict):
            return [option]
    if isinstance(options, list):
        return [item for item in options if isinstance(item, dict)]
    return []


def _extract_buyer_inputs(payload: dict[str, Any]) -> dict[str, str]:
    buyer_inputs: dict[str, str] = {}
    option_index = 1
    for option in _extract_options(payload):
        value = _first_non_empty_str(option, ("user_data", "value", "text"))
        if not value:
            continue
        if _to_int(option.get("user_data_id")) is not None:
            continue
        label = _first_non_empty_str(option, ("name", "label", "title"))
        key = _normalize_input_key(label, f"input_{option_index}")
        buyer_inputs[key] = value
        option_index += 1

    buyer_info = payload.get("buyer_info")
    if isinstance(buyer_info, dict):
        # buyer_info.account is the buyer's payment system ID, not a product input —
        # prefix it to avoid colliding with the actual product "account" field.
        for raw_key in ("info",):
            value = buyer_info.get(raw_key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                buyer_inputs.setdefault(raw_key, text)
    return buyer_inputs


def _extract_recharge_option_inputs(payload: dict[str, Any]) -> dict[str, str]:
    option_inputs: dict[str, str] = {}
    option_index = 1
    for option in _extract_options(payload):
        value = _first_non_empty_str(option, ("user_data", "value", "text"))
        if not value:
            continue
        variant_id = _to_int(
            _first_non_empty_str(
                option,
                ("user_data_id", "variant_id", "variantId", "value_id", "id_variant"),
            )
        )
        if variant_id is None:
            continue
        label = _first_non_empty_str(option, ("name", "label", "title"))
        option_id = _first_non_empty_str(option, ("id", "option_id", "optionId"))
        fallback = f"option_{option_id}" if option_id else f"option_{option_index}"
        key = _canonical_recharge_option_key(label, fallback)
        if key == "quantity":
            parsed_quantity = _extract_numeric_quantity(value)
            option_inputs[key] = parsed_quantity or value
        else:
            option_inputs[key] = value
        option_index += 1
    return option_inputs


def _extract_variant_id(payload: dict[str, Any]) -> int | None:
    direct = _to_int(
        _first_non_empty_str(
            payload,
            (
                "variant_id",
                "variantId",
                "id_variant",
                "id_var",
                "option_value_id",
                "user_data_id",
            ),
        )
    )
    if direct is not None:
        return direct

    for option in _extract_options(payload):
        variant_id = _to_int(
            _first_non_empty_str(
                option,
                ("user_data_id", "variant_id", "variantId", "value_id", "id_variant"),
            )
        )
        if variant_id is not None:
            return variant_id
    return None


def _extract_variant_text(payload: dict[str, Any]) -> str | None:
    for option in _extract_options(payload):
        if _to_int(option.get("user_data_id")) is None:
            continue
        value = _first_non_empty_str(option, ("user_data", "value", "text"))
        if value:
            return value
    return _first_non_empty_str(payload, ("variant", "variant_name", "option", "denomination"))


def _extract_unique_code(payload: dict[str, Any], explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    direct = _first_non_empty_str(
        payload,
        ("unique_code", "uniquecode", "uniqueCode", "code_unique", "unique_code_value"),
    )
    if direct:
        return direct

    query_string = _first_non_empty_str(payload, ("query_string", "queryString"))
    if query_string:
        parsed = parse_qs(query_string.lstrip("?"), keep_blank_values=False)
        for key in ("uniquecode", "unique_code", "uniqueCode"):
            values = parsed.get(key)
            if values:
                value = str(values[0]).strip()
                if value:
                    return value
    return None


def _unique_code_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"\b[A-Za-z0-9][A-Za-z0-9_-]{7,63}\b", text):
        candidate = match.group(0).strip("._-")
        if len(candidate) < 8:
            continue
        if candidate.isdigit():
            continue
        if not any(character.isdigit() for character in candidate):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _message_bool(value: object) -> bool | None:
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "buyer", "customer", "client"}:
        return True
    if text in {"0", "false", "no", "seller", "vendor", "merchant"}:
        return False
    return None


def _message_is_from_buyer(message: dict[str, Any]) -> bool:
    for key in ("buyer", "is_buyer", "from_buyer", "isBuyer", "fromBuyer"):
        parsed = _message_bool(message.get(key))
        if parsed is not None:
            return parsed

    for key in ("seller", "is_seller", "from_seller", "isSeller", "fromSeller"):
        parsed = _message_bool(message.get(key))
        if parsed is not None:
            return not parsed

    for key in ("role", "author", "sender", "sender_type", "senderType", "side"):
        parsed = _message_bool(message.get(key))
        if parsed is not None:
            return parsed

    # Verification with Digiseller still gates delivery, so unknown-origin chat
    # rows are worth scanning when the buyer sends the code before our prompt.
    return True


def _unique_code_candidates_from_messages(messages: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    ordered_messages = sorted(
        messages,
        key=lambda item: str(item.get("date_written") or item.get("date") or ""),
        reverse=True,
    )
    for message in ordered_messages:
        if str(message.get("deleted") or "0").strip().lower() in {"1", "true", "yes"}:
            continue
        if not _message_is_from_buyer(message):
            continue
        text = str(message.get("message") or message.get("text") or "").strip()
        for candidate in _unique_code_candidates_from_text(text):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _sale_is_paid(payload: dict[str, Any]) -> bool:
    returned = str(payload.get("returned") or payload.get("refund") or "").strip().lower()
    if returned in {"1", "true", "yes", "returned", "refunded"}:
        return False

    status = str(
        payload.get("status")
        or payload.get("state")
        or payload.get("payment_status")
        or payload.get("invoice_state")
        or ""
    ).strip().lower()
    if status in {"cancelled", "canceled", "refunded", "returned", "refund"}:
        return False
    if status in {"paid", "complete", "completed", "success", "successful", "sold", "2", "3"}:
        return True

    if _first_non_empty_str(payload, ("date_pay", "paid_at", "payment_date")) is not None:
        return True
    return True


def _normalize_marketplace_sale(
    marketplace: str,
    *payloads: dict[str, Any],
    unique_code: str | None = None,
) -> NormalizedMarketplaceSale:
    payload = _merge_payloads(*payloads)
    order_id = _first_non_empty_str(
        payload,
        (
            "marketplace_order_id",
            "invoice_id",
            "invoiceID",
            "invoiceId",
            "inv",
            "id_i",
            "order_id",
            "orderId",
            "id",
        ),
    )
    offer_id = _first_non_empty_str(
        payload,
        (
            "marketplace_offer_id",
            "id_goods",
            "goods_id",
            "good_id",
            "item_id",
            "product_id",
            "productId",
            "id_product",
        ),
    )
    buyer_id = _first_non_empty_str(
        payload,
        ("buyer_id", "buyerId", "email", "buyer_email", "account", "owner"),
    )
    quantity = _to_int(_first_non_empty_str(payload, ("cnt_goods", "quantity", "count"))) or 1
    if quantity < 1:
        quantity = 1

    return NormalizedMarketplaceSale(
        marketplace=marketplace,
        order_id=order_id,
        offer_id=offer_id,
        variant_id=_extract_variant_id(payload),
        variant_text=_extract_variant_text(payload),
        buyer_id=buyer_id,
        sale_price=(
            _to_decimal(payload.get("amount"))
            or _to_decimal(payload.get("amount_usd"))
            or _to_decimal(payload.get("amount_in"))
            or _to_decimal(payload.get("amount_out"))
            or _to_decimal(payload.get("price"))
        ),
        quantity=quantity,
        paid=_sale_is_paid(payload),
        unique_code=_extract_unique_code(payload, unique_code),
        buyer_inputs=_extract_buyer_inputs(payload),
        raw_payload=payload,
    )


def _load_offer(
    db: Session,
    *,
    marketplace: str,
    offer_id: str,
) -> MarketplaceOffer | None:
    stmt = (
        select(MarketplaceOffer)
        .options(
            selectinload(MarketplaceOffer.mappings).selectinload(ProductMapping.supplier_product)
        )
        .where(
            MarketplaceOffer.marketplace == marketplace,
            MarketplaceOffer.marketplace_offer_id == offer_id,
        )
    )
    return db.execute(stmt).scalars().first()


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _select_mapping(
    offer: MarketplaceOffer,
    sale: NormalizedMarketplaceSale,
) -> tuple[ProductMapping | None, str | None]:
    enabled_mappings = [mapping for mapping in offer.mappings if mapping.enabled]
    if not enabled_mappings:
        return None, "offer_has_no_enabled_mappings"

    def _single_mapping_fallback_for_recharge() -> ProductMapping | None:
        if len(enabled_mappings) != 1:
            return None
        mapping = enabled_mappings[0]
        supplier_id = str(getattr(mapping.supplier_product, "supplier_product_id", "") or "").strip()
        parts = supplier_id.split(":")
        is_recharge_service = len(parts) == 2 and parts[0] == "recharge" and bool(parts[1])
        if is_recharge_service:
            return mapping
        if mapping.marketplace_variant_id is None:
            return mapping
        return None

    if sale.variant_id is not None:
        for mapping in enabled_mappings:
            if mapping.marketplace_variant_id == sale.variant_id:
                return mapping, None
        fallback_mapping = _single_mapping_fallback_for_recharge()
        if fallback_mapping is not None:
            return fallback_mapping, None
        # Additive quantity fallback when variant_id doesn't match any mapping:
        # "35 Diamonds" (user_data) matches mapping with name "32 + 3 Diamonds" (32+3=35)
        if sale.variant_text:
            sale_qty = _parse_additive_quantity(sale.variant_text)
            if sale_qty is not None:
                for mapping in enabled_mappings:
                    if _parse_additive_quantity(mapping.marketplace_variant_name) == sale_qty:
                        return mapping, None
        return None, f"variant_mapping_not_found:{sale.variant_id}"

    if sale.variant_text:
        sale_variant_text = _normalized_text(sale.variant_text)
        for mapping in enabled_mappings:
            if _normalized_text(mapping.marketplace_variant_name) == sale_variant_text:
                return mapping, None
        # Additive quantity fallback: "35 Diamonds" matches "32 + 3 Diamonds" (32+3=35)
        sale_qty = _parse_additive_quantity(sale.variant_text)
        if sale_qty is not None:
            for mapping in enabled_mappings:
                if _parse_additive_quantity(mapping.marketplace_variant_name) == sale_qty:
                    return mapping, None

    if len(enabled_mappings) == 1:
        return enabled_mappings[0], None
    return None, "multiple_mappings_require_marketplace_variant_id"


def _get_or_create_order(
    db: Session,
    sale: NormalizedMarketplaceSale,
    offer: MarketplaceOffer,
    mapping: ProductMapping,
) -> Order:
    stmt = select(Order).where(
        Order.marketplace == sale.marketplace,
        Order.marketplace_order_id == sale.order_id,
    )
    existing = db.execute(stmt).scalars().first()
    if existing is not None:
        existing.marketplace_offer_id = offer.id
        existing.supplier_product_id = mapping.supplier_product_id
        if sale.buyer_id:
            existing.buyer_id = sale.buyer_id
        if sale.sale_price is not None:
            existing.sale_price = sale.sale_price
        db.add(existing)
        db.flush()
        return existing

    order = Order(
        marketplace=sale.marketplace,
        marketplace_order_id=cast(str, sale.order_id),
        marketplace_offer_id=offer.id,
        supplier_product_id=mapping.supplier_product_id,
        buyer_id=sale.buyer_id,
        status="pending",
        sale_price=sale.sale_price,
    )
    db.add(order)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return db.execute(stmt).scalars().one()
    return order


def _claim_order_for_supplier_purchase(db: Session, order: Order) -> bool:
    result = db.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status == "pending",
            Order.supplier_order_id.is_(None),
        )
        .values(status="processing", error_message=None)
    )
    db.commit()
    db.refresh(order)
    return int(getattr(result, "rowcount", 0) or 0) == 1


def _first_path_value(payload: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> object | None:
    for path in paths:
        current: object = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
            if current is None:
                break
        if current is not None:
            return current
    return None


def _extract_supplier_order_id(payload: dict[str, Any], fallback: str) -> str:
    value = _first_path_value(
        payload,
        (
            ("order_id",),
            ("orderId",),
            ("supplier_order_id",),
            ("id_order",),
            ("data", "order_id"),
            ("data", "id"),
            ("order", "id"),
            ("result", "order_id"),
            ("result", "id"),
            ("id",),
        ),
    )
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


_CODE_KEYS = {
    "code",
    "codes",
    "pin",
    "pins",
    "serial",
    "serials",
    "key",
    "keys",
    "activation_code",
    "activation_codes",
    "voucher",
    "vouchers",
    "claim_code",
    "claim_codes",
}
_EXCLUDED_CODE_KEYS = {"unique_code", "uniquecode", "unique_code_state"}


def _collect_delivery_codes(value: object, *, key_hint: str | None, codes: list[str]) -> None:
    normalized_key = str(key_hint or "").strip().lower()
    key_is_code = normalized_key in _CODE_KEYS or (
        normalized_key.endswith("_code") and normalized_key not in _EXCLUDED_CODE_KEYS
    )
    if isinstance(value, dict):
        for key, nested in value.items():
            _collect_delivery_codes(nested, key_hint=str(key), codes=codes)
        return
    if isinstance(value, list):
        for item in value:
            _collect_delivery_codes(item, key_hint=normalized_key, codes=codes)
        return
    if not key_is_code or value is None:
        return
    text = str(value).strip()
    if len(text) >= 4 and text not in codes:
        codes.append(text)


def _extract_delivery_codes(payload: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    _collect_delivery_codes(payload, key_hint=None, codes=codes)
    return codes


def _supplier_payload_status(payload: dict[str, Any]) -> str:
    value = _first_path_value(
        payload,
        (
            ("status",),
            ("state",),
            ("data", "status"),
            ("data", "state"),
            ("content", "status"),
            ("content", "state"),
            ("order", "status"),
            ("order", "state"),
            ("result", "status"),
            ("result", "state"),
        ),
    )
    return str(value or "").strip().lower()


def _supplier_payload_is_fulfilled(payload: dict[str, Any]) -> bool:
    status = _supplier_payload_status(payload)
    return status in {
        "completed",
        "complete",
        "fulfilled",
        "delivered",
        "success",
        "successful",
        "shipped",
    }


def _supplier_payload_terminal_failure(payload: dict[str, Any]) -> str | None:
    status = _supplier_payload_status(payload)
    if status in {"cancelled", "canceled", "expired", "failed", "error", "refunded", "returned"}:
        return f"supplier_order_terminal:{status}"
    return None


def _supplier_raw_payload(supplier: Any) -> dict[str, Any]:
    raw_payload_json = getattr(supplier, "raw_payload_json", None)
    if not raw_payload_json:
        return {}
    try:
        payload = json.loads(str(raw_payload_json))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _delivery_product_title(
    offer: MarketplaceOffer,
    mapping: ProductMapping,
    sale: NormalizedMarketplaceSale,
) -> str:
    supplier = mapping.supplier_product
    title = str(offer.title or supplier.title or "your product").strip()
    variant = str(sale.variant_text or mapping.marketplace_variant_name or "").strip()
    if variant and _normalized_text(variant) not in _normalized_text(title):
        return f"{title} - {variant}"
    return title


def _delivery_message_context(
    offer: MarketplaceOffer,
    mapping: ProductMapping,
    sale: NormalizedMarketplaceSale,
) -> tuple[str, DeliveryProductKind]:
    supplier = mapping.supplier_product
    product_title = _delivery_product_title(offer, mapping, sale)
    kind = detect_delivery_product_kind(
        supplier_product_id=supplier.supplier_product_id,
        product_title=f"{product_title} {supplier.title}",
        product_category=supplier.category,
        product_payload=_supplier_raw_payload(supplier),
    )
    return product_title, kind


def _send_purchase_acknowledgement(
    *,
    order: Order,
    sale: NormalizedMarketplaceSale,
    marketplace_client: MarketplaceOrderClientProtocol,
    product_title: str,
    product_kind: DeliveryProductKind,
    stats: dict[str, Any],
) -> None:
    buyer_id = sale.buyer_id or order.buyer_id or ""
    message = build_purchase_message(product_title=product_title, product_kind=product_kind)
    try:
        marketplace_client.send_buyer_message(order.marketplace_order_id, buyer_id, message)
    except Exception as exc:  # noqa: BLE001 - acknowledgement must not block fulfillment.
        stats["details"].append(
            {"action": "purchase_message_failed", "order_id": order.id, "reason": str(exc)}
        )


def _send_provider_unavailable_notice(
    *,
    order: Order,
    sale: NormalizedMarketplaceSale,
    marketplace_client: MarketplaceOrderClientProtocol,
    product_title: str,
    stats: dict[str, Any],
) -> bool:
    if order.error_message == SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED:
        return True

    buyer_id = sale.buyer_id or order.buyer_id or ""
    message = build_provider_unavailable_message(product_title=product_title)
    try:
        marketplace_client.send_buyer_message(order.marketplace_order_id, buyer_id, message)
    except Exception as exc:  # noqa: BLE001 - keep the order retryable even if chat fails.
        stats["details"].append(
            {
                "action": "provider_unavailable_message_failed",
                "order_id": order.id,
                "reason": str(exc),
            }
        )
        return False
    return True


def _defer_for_provider_unavailable(
    *,
    db: Session,
    order: Order,
    sale: NormalizedMarketplaceSale,
    offer: MarketplaceOffer,
    mapping: ProductMapping,
    marketplace_client: MarketplaceOrderClientProtocol,
    stats: dict[str, Any],
    retry_status: OrderStatus,
) -> None:
    product_title, _product_kind = _delivery_message_context(offer, mapping, sale)
    notice_sent = _send_provider_unavailable_notice(
        order=order,
        sale=sale,
        marketplace_client=marketplace_client,
        product_title=product_title,
        stats=stats,
    )
    order.status = retry_status
    order.error_message = (
        SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED if notice_sent else SUPPLIER_PROVIDER_UNAVAILABLE
    )
    db.add(order)
    db.commit()
    stats["provider_unavailable"] += 1
    stats["skipped"] += 1
    stats["details"].append(
        {
            "action": "skipped",
            "order_id": order.id,
            "marketplace_order_id": order.marketplace_order_id,
            "reason": SUPPLIER_PROVIDER_UNAVAILABLE,
        }
    )


def _create_supplier_purchase(
    *,
    order: Order,
    mapping: ProductMapping,
    sale: NormalizedMarketplaceSale,
    supplier_client: SupplierOrderClientProtocol,
) -> dict[str, Any]:
    supplier = mapping.supplier_product
    external_order_id = f"{sale.marketplace}:{sale.order_id}"
    buyer_inputs = dict(sale.buyer_inputs)
    if str(supplier.supplier_product_id).startswith("recharge:"):
        recharge_option_inputs = _extract_recharge_option_inputs(sale.raw_payload)
        for key, value in recharge_option_inputs.items():
            buyer_inputs.setdefault(key, value)
    response = supplier_client.create_order(
        supplier.supplier_product_id,
        sale.quantity,
        buyer_inputs=buyer_inputs,
        external_order_id=external_order_id,
        product_payload=_supplier_raw_payload(supplier),
    )
    order.supplier_order_id = _extract_supplier_order_id(response, fallback=external_order_id)
    order.cost_usd = supplier.cost_usd * sale.quantity
    order.status = "supplier_order_created"
    order.error_message = None
    return response


def _load_supplier_delivery_payload(
    *,
    order: Order,
    supplier_client: SupplierOrderClientProtocol,
    create_response: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], bool, str | None]:
    payload = create_response if isinstance(create_response, dict) else {}
    codes = _extract_delivery_codes(payload)
    fulfilled = bool(codes) or _supplier_payload_is_fulfilled(payload)
    terminal_failure = _supplier_payload_terminal_failure(payload)
    if fulfilled or terminal_failure is not None or not order.supplier_order_id:
        return payload, codes, fulfilled, terminal_failure

    status_payload = supplier_client.get_order_status(order.supplier_order_id)
    codes = _extract_delivery_codes(status_payload)
    fulfilled = bool(codes) or _supplier_payload_is_fulfilled(status_payload)
    terminal_failure = _supplier_payload_terminal_failure(status_payload)
    return status_payload, codes, fulfilled, terminal_failure


def _digiseller_unique_code_state(payload: dict[str, Any]) -> int | None:
    state_payload = payload.get("unique_code_state")
    if isinstance(state_payload, dict):
        state = _to_int(state_payload.get("state"))
        if state is not None:
            return state
    state = _to_int(payload.get("unique_code_state"))
    if state is not None:
        return state
    content = payload.get("content")
    if isinstance(content, dict):
        return _digiseller_unique_code_state(content)
    return None


def _digiseller_unique_code_is_already_delivered(payload: dict[str, Any]) -> bool:
    return _digiseller_unique_code_state(payload) in {2, 3}


def _digiseller_verified_payload_matches_order(
    payload: dict[str, Any],
    sale: NormalizedMarketplaceSale,
) -> bool:
    verified = _normalize_marketplace_sale("digiseller", payload, unique_code=sale.unique_code)
    if verified.order_id and sale.order_id and verified.order_id != sale.order_id:
        return False
    if verified.offer_id and sale.offer_id and verified.offer_id != sale.offer_id:
        return False
    return True


def _preverify_digiseller_unique_code(
    *,
    sale: NormalizedMarketplaceSale,
    marketplace_client: MarketplaceOrderClientProtocol,
) -> tuple[NormalizedMarketplaceSale | None, str | None]:
    if sale.marketplace != "digiseller":
        return sale, None
    digiseller_client = cast(DigisellerDeliveryClientProtocol, marketplace_client)
    verification_payload: dict[str, Any] | None = None
    unique_code = sale.unique_code

    if unique_code:
        verification_payload = digiseller_client.verify_unique_code(unique_code)
        if not _digiseller_verified_payload_matches_order(verification_payload, sale):
            return None, "digiseller_unique_code_does_not_match_order"
    elif sale.order_id:
        try:
            messages = digiseller_client.get_buyer_messages(sale.order_id)
        except Exception as exc:  # noqa: BLE001 - buyer can post the code before the next run.
            return None, f"awaiting_digiseller_unique_code_chat_lookup_failed:{exc}"
        for candidate in _unique_code_candidates_from_messages(messages)[:10]:
            try:
                candidate_payload = digiseller_client.verify_unique_code(candidate)
            except Exception:
                continue
            if _digiseller_verified_payload_matches_order(candidate_payload, sale):
                unique_code = candidate
                verification_payload = candidate_payload
                break

    if not unique_code or verification_payload is None:
        return None, "awaiting_digiseller_unique_code"

    verified_sale = _normalize_marketplace_sale(
        "digiseller",
        sale.raw_payload,
        verification_payload,
        unique_code=unique_code,
    )
    verified_sale = replace(
        verified_sale,
        order_id=sale.order_id or verified_sale.order_id,
        offer_id=sale.offer_id or verified_sale.offer_id,
        variant_id=sale.variant_id if sale.variant_id is not None else verified_sale.variant_id,
        variant_text=sale.variant_text or verified_sale.variant_text,
        buyer_id=sale.buyer_id or verified_sale.buyer_id,
        sale_price=sale.sale_price or verified_sale.sale_price,
        quantity=sale.quantity or verified_sale.quantity,
        buyer_inputs={**sale.buyer_inputs, **verified_sale.buyer_inputs},
    )
    verified_sale.raw_payload["_digiseller_unique_code_preverified"] = True
    return verified_sale, None


def _deliver_to_marketplace(
    *,
    order: Order,
    offer: MarketplaceOffer,
    mapping: ProductMapping,
    sale: NormalizedMarketplaceSale,
    marketplace_client: MarketplaceOrderClientProtocol,
    codes: list[str],
) -> None:
    product_title, product_kind = _delivery_message_context(offer, mapping, sale)
    message = build_delivery_message(
        codes,
        product_title=product_title,
        product_kind=product_kind,
    )
    buyer_id = sale.buyer_id or order.buyer_id or ""

    if sale.marketplace == "digiseller":
        if not sale.unique_code:
            order.status = "supplier_order_created"
            order.error_message = "awaiting_digiseller_unique_code"
            return
        digiseller_client = cast(DigisellerDeliveryClientProtocol, marketplace_client)
        if bool(sale.raw_payload.get("_digiseller_unique_code_preverified")):
            verification_payload = sale.raw_payload
        else:
            verification_payload = digiseller_client.verify_unique_code(sale.unique_code)
            if not _digiseller_verified_payload_matches_order(verification_payload, sale):
                order.status = "needs_manual_review"
                order.error_message = "digiseller_unique_code_does_not_match_order"
                return
        if not _digiseller_unique_code_is_already_delivered(verification_payload):
            marketplace_client.send_buyer_message(order.marketplace_order_id, buyer_id, message)
            digiseller_client.mark_unique_code_delivered(sale.unique_code)
    else:
        marketplace_client.send_buyer_message(order.marketplace_order_id, buyer_id, message)

    order.status = "delivered"
    order.delivered_code_encrypted_or_masked = (
        "\n".join(masked_code_for_logs(code) for code in codes)
        if codes
        else "fulfilled_without_code"
    )
    order.error_message = None


def _process_normalized_sale(
    *,
    db: Session,
    sale: NormalizedMarketplaceSale,
    supplier_client: SupplierOrderClientProtocol,
    marketplace_client: MarketplaceOrderClientProtocol,
    stats: dict[str, Any],
    allow_supplier_recreate: bool = True,
) -> None:
    if not sale.order_id or not sale.offer_id:
        stats["skipped"] += 1
        stats["details"].append({"action": "skipped", "reason": "missing_order_or_offer_id"})
        return
    if not sale.paid:
        stats["skipped"] += 1
        stats["details"].append(
            {"action": "skipped", "order_id": sale.order_id, "reason": "order_not_paid"}
        )
        return

    offer = _load_offer(db, marketplace=sale.marketplace, offer_id=sale.offer_id)
    if offer is None:
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "marketplace": sale.marketplace,
                "marketplace_order_id": sale.order_id,
                "marketplace_offer_id": sale.offer_id,
                "reason": "marketplace_offer_not_linked",
            }
        )
        return

    mapping, mapping_error = _select_mapping(offer, sale)
    if mapping is None:
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "marketplace": sale.marketplace,
                "marketplace_order_id": sale.order_id,
                "marketplace_offer_id": sale.offer_id,
                "variant_id": sale.variant_id,
                "reason": mapping_error,
            }
        )
        return

    order = _get_or_create_order(db, sale, offer, mapping)
    db.commit()

    if order.status == "delivered":
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "reason": "order_already_delivered",
            }
        )
        return

    prev_error_message = order.error_message
    verified_sale, verification_error = _preverify_digiseller_unique_code(
        sale=sale,
        marketplace_client=marketplace_client,
    )
    if verified_sale is None:
        order.status = "needs_manual_review" if verification_error == "digiseller_unique_code_does_not_match_order" else "pending"
        order.error_message = verification_error
        waiting_for_code = _awaiting_digiseller_unique_code(verification_error)
        prompt_was_sent = _digiseller_unique_code_prompt_was_sent(prev_error_message)
        if waiting_for_code and prompt_was_sent:
            order.error_message = _mark_digiseller_unique_code_prompt_sent(verification_error)
        if waiting_for_code and not prompt_was_sent:
            buyer_id = sale.buyer_id or order.buyer_id or ""
            try:
                marketplace_client.send_buyer_message(
                    order.marketplace_order_id,
                    buyer_id,
                    build_unique_code_request_message(),
                )
                order.error_message = _mark_digiseller_unique_code_prompt_sent(verification_error)
            except Exception as exc:  # noqa: BLE001 - prompt must not block the pending state.
                stats["details"].append(
                    {
                        "action": "unique_code_prompt_failed",
                        "order_id": order.id,
                        "reason": str(exc),
                    }
                )
        db.add(order)
        db.commit()
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "reason": verification_error,
            }
        )
        return
    sale = verified_sale

    if (
        order.status == "needs_manual_review"
        and _supplier_order_can_be_recreated(order.error_message)
    ):
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "reason": "supplier_order_terminal_requires_manual_retry",
            }
        )
        return

    create_response: dict[str, Any] | None = None
    just_created = False
    if (
        allow_supplier_recreate
        and order.supplier_order_id
        and _sale_allows_supplier_recreate(sale)
        and _supplier_order_can_be_recreated(order.error_message)
    ):
        previous_supplier_order_id = str(order.supplier_order_id or "")
        order.supplier_order_id = None
        order.status = "pending"
        order.error_message = None
        db.add(order)
        db.commit()
        stats["details"].append(
            {
                "action": "supplier_order_recreate_queued",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "previous_supplier_order_id": previous_supplier_order_id,
                "reason": "previous_supplier_order_terminal",
            }
        )

    if not order.supplier_order_id:
        if not should_create_supplier_purchase(order.status):
            stats["skipped"] += 1
            stats["details"].append(
                {
                    "action": "skipped",
                    "order_id": order.id,
                    "marketplace_order_id": order.marketplace_order_id,
                    "reason": f"supplier_purchase_not_retryable:{order.status}",
                }
            )
            return
        if not _claim_order_for_supplier_purchase(db, order):
            stats["skipped"] += 1
            stats["details"].append(
                {
                    "action": "skipped",
                    "order_id": order.id,
                    "marketplace_order_id": order.marketplace_order_id,
                    "reason": f"supplier_purchase_already_claimed:{order.status}",
                }
            )
            return
        try:
            create_response = _create_supplier_purchase(
                order=order,
                mapping=mapping,
                sale=sale,
                supplier_client=supplier_client,
            )
        except Exception as exc:  # noqa: BLE001 - keep other paid orders moving.
            if is_temporary_provider_error(exc):
                product_title, _product_kind = _delivery_message_context(offer, mapping, sale)
                notice_sent = _send_provider_unavailable_notice(
                    order=order,
                    sale=sale,
                    marketplace_client=marketplace_client,
                    product_title=product_title,
                    stats=stats,
                )
                order.status = "needs_manual_review"
                order.error_message = f"{_SUPPLIER_PURCHASE_STATUS_UNKNOWN}:{exc}"
                if notice_sent:
                    order.error_message = f"{order.error_message};buyer_notified"
                db.add(order)
                db.commit()
                stats["provider_unavailable"] += 1
                stats["errors"] += 1
                stats["details"].append(
                    {
                        "action": "error",
                        "order_id": order.id,
                        "marketplace_order_id": order.marketplace_order_id,
                        "reason": _SUPPLIER_PURCHASE_STATUS_UNKNOWN,
                    }
                )
                return
            order.status = "needs_manual_review"
            order.error_message = f"{_SUPPLIER_PURCHASE_STATUS_UNKNOWN}:{exc}"
            db.add(order)
            db.commit()
            stats["errors"] += 1
            stats["details"].append(
                {
                    "action": "error",
                    "order_id": order.id,
                    "marketplace_order_id": order.marketplace_order_id,
                    "reason": f"{_SUPPLIER_PURCHASE_STATUS_UNKNOWN}:{exc}",
                }
            )
            return
        db.add(order)
        db.commit()
        stats["supplier_orders_created"] += 1
        product_title, product_kind = _delivery_message_context(offer, mapping, sale)
        just_created = True

    try:
        _payload, codes, fulfilled, terminal_failure = _load_supplier_delivery_payload(
            order=order,
            supplier_client=supplier_client,
            create_response=create_response,
        )
        if terminal_failure is not None:
            if (
                allow_supplier_recreate
                and _sale_allows_supplier_recreate(sale)
                and _supplier_order_can_be_recreated(terminal_failure)
            ):
                previous_supplier_order_id = str(order.supplier_order_id or "")
                order.supplier_order_id = None
                order.status = "pending"
                order.error_message = None
                db.add(order)
                db.commit()
                stats["details"].append(
                    {
                        "action": "supplier_order_recreate_queued",
                        "order_id": order.id,
                        "marketplace_order_id": order.marketplace_order_id,
                        "previous_supplier_order_id": previous_supplier_order_id,
                        "reason": terminal_failure,
                    }
                )
                _process_normalized_sale(
                    db=db,
                    sale=sale,
                    supplier_client=supplier_client,
                    marketplace_client=marketplace_client,
                    stats=stats,
                    allow_supplier_recreate=False,
                )
                return

            order.status = "needs_manual_review"
            order.error_message = terminal_failure
            db.add(order)
            db.commit()
            stats["errors"] += 1
            stats["details"].append(
                {
                    "action": "error",
                    "order_id": order.id,
                    "marketplace_order_id": order.marketplace_order_id,
                    "reason": terminal_failure,
                }
            )
            return

        if not fulfilled:
            if just_created:
                _send_purchase_acknowledgement(
                    order=order,
                    sale=sale,
                    marketplace_client=marketplace_client,
                    product_title=product_title,
                    product_kind=product_kind,
                    stats=stats,
                )
            order.status = "supplier_order_created"
            order.error_message = "supplier_order_not_ready"
            db.add(order)
            db.commit()
            stats["skipped"] += 1
            stats["details"].append(
                {
                    "action": "skipped",
                    "order_id": order.id,
                    "marketplace_order_id": order.marketplace_order_id,
                    "reason": "supplier_order_not_ready",
                }
            )
            return

        _deliver_to_marketplace(
            order=order,
            offer=offer,
            mapping=mapping,
            sale=sale,
            marketplace_client=marketplace_client,
            codes=codes,
        )
    except Exception as exc:  # noqa: BLE001 - preserve supplier purchase for delivery retry.
        if is_temporary_provider_error(exc):
            if just_created:
                _send_purchase_acknowledgement(
                    order=order,
                    sale=sale,
                    marketplace_client=marketplace_client,
                    product_title=product_title,
                    product_kind=product_kind,
                    stats=stats,
                )
            _defer_for_provider_unavailable(
                db=db,
                order=order,
                sale=sale,
                offer=offer,
                mapping=mapping,
                marketplace_client=marketplace_client,
                stats=stats,
                retry_status="supplier_order_created" if order.supplier_order_id else "pending",
            )
            return
        order.status = "failed" if not order.supplier_order_id else "needs_manual_review"
        order.error_message = str(exc)
        db.add(order)
        db.commit()
        stats["errors"] += 1
        stats["details"].append(
            {
                "action": "error",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "reason": f"delivery_failed:{exc}",
            }
        )
        return

    db.add(order)
    db.commit()
    if order.status == "delivered":
        stats["orders_delivered"] += 1
        stats["orders_processed"] += 1
        stats["details"].append({"action": "delivered", "order_id": order.id})
    else:
        stats["skipped"] += 1
        stats["details"].append(
            {
                "action": "skipped",
                "order_id": order.id,
                "marketplace_order_id": order.marketplace_order_id,
                "reason": order.error_message,
            }
        )


def _process_raw_sale(
    *,
    db: Session,
    marketplace: str,
    raw_sale: dict[str, Any],
    supplier_client: SupplierOrderClientProtocol,
    marketplace_client: MarketplaceOrderClientProtocol,
    stats: dict[str, Any],
) -> None:
    summary = _normalize_marketplace_sale(marketplace, raw_sale)
    detail_payload: dict[str, Any] = {}
    if summary.order_id:
        try:
            detail_payload = marketplace_client.get_order(summary.order_id)
        except Exception as exc:  # noqa: BLE001 - summary can still be enough.
            stats["details"].append(
                {
                    "action": "order_detail_lookup_failed",
                    "marketplace": marketplace,
                    "marketplace_order_id": summary.order_id,
                    "reason": str(exc),
                }
            )

    sale = _normalize_marketplace_sale(
        marketplace,
        raw_sale,
        detail_payload,
        unique_code=summary.unique_code,
    )
    _process_normalized_sale(
        db=db,
        sale=sale,
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        stats=stats,
    )


def _last_successful_order_sync_time(db: Session) -> datetime | None:
    stmt = (
        select(SyncRun)
        .where(SyncRun.job_name == "order_sync", SyncRun.status == "success")
        .order_by(SyncRun.finished_at.desc(), SyncRun.started_at.desc())
        .limit(1)
    )
    sync_run = db.execute(stmt).scalars().first()
    if sync_run is None:
        return None
    return sync_run.finished_at or sync_run.started_at


def _order_poll_window(db: Session, settings: Settings) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    last_success = _last_successful_order_sync_time(db)
    if last_success is None:
        start = now - timedelta(hours=settings.order_sync_initial_lookback_hours)
    else:
        start = last_success - timedelta(minutes=settings.order_sync_overlap_minutes)
    return start, now + timedelta(minutes=settings.order_sync_future_window_minutes)


def _load_retryable_pending_order_ids(
    db: Session,
    *,
    marketplace: str,
    exclude_order_ids: set[str],
) -> list[str]:
    retry_conditions = [
        Order.error_message.like("awaiting_digiseller_unique_code%"),
        Order.error_message == "supplier_order_not_ready",
        Order.error_message.in_(
            (SUPPLIER_PROVIDER_UNAVAILABLE, SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED)
        ),
        and_(Order.status == "pending", Order.supplier_order_id.is_(None)),
    ]
    statuses = ["pending", "supplier_order_created"]

    stmt = (
        select(Order.marketplace_order_id)
        .where(
            Order.marketplace == marketplace,
            Order.marketplace_order_id.not_in(exclude_order_ids),
            Order.status.in_(statuses),
            or_(*retry_conditions),
        )
        .order_by(Order.created_at)
        .limit(200)
    )
    return [str(order_id) for order_id in db.execute(stmt).scalars().all()]


def sync_orders(
    *,
    db: Session,
    settings: Settings,
    supplier_client: SupplierOrderClientProtocol,
    marketplace_clients: dict[str, MarketplaceOrderClientProtocol],
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "status": "ok",
        "orders_detected": 0,
        "orders_processed": 0,
        "supplier_orders_created": 0,
        "orders_delivered": 0,
        "skipped": 0,
        "errors": 0,
        "provider_unavailable": 0,
        "details": [],
    }
    date_start, date_finish = _order_poll_window(db, settings)

    for marketplace, marketplace_client in marketplace_clients.items():
        try:
            sales = marketplace_client.get_latest_sales(
                date_start=date_start,
                date_finish=date_finish,
            )
        except Exception as exc:  # noqa: BLE001 - one marketplace should not block the other.
            stats["errors"] += 1
            stats["details"].append(
                {
                    "action": "error",
                    "marketplace": marketplace,
                    "reason": f"latest_sales_failed:{exc}",
                }
            )
            continue

        seen_order_ids: set[str] = set()
        for raw_sale in sales:
            stats["orders_detected"] += 1
            summary = _normalize_marketplace_sale(marketplace, raw_sale)
            if summary.order_id:
                seen_order_ids.add(summary.order_id)
            _process_raw_sale(
                db=db,
                marketplace=marketplace,
                raw_sale=raw_sale,
                supplier_client=supplier_client,
                marketplace_client=marketplace_client,
                stats=stats,
            )

        pending_order_ids = _load_retryable_pending_order_ids(
            db,
            marketplace=marketplace,
            exclude_order_ids=seen_order_ids,
        )
        for marketplace_order_id in pending_order_ids:
            stats["orders_detected"] += 1
            _process_raw_sale(
                db=db,
                marketplace=marketplace,
                raw_sale={"marketplace_order_id": marketplace_order_id},
                supplier_client=supplier_client,
                marketplace_client=marketplace_client,
                stats=stats,
            )

    if stats["errors"] > 0 or stats["provider_unavailable"] > 0:
        stats["status"] = "partial"
    return stats


def process_digiseller_unique_code(
    *,
    unique_code: str,
    db: Session,
    settings: Settings,
    supplier_client: SupplierOrderClientProtocol,
    marketplace_client: DigisellerDeliveryClientProtocol,
) -> dict[str, Any]:
    _ = settings
    stats: dict[str, Any] = {
        "status": "ok",
        "orders_detected": 1,
        "orders_processed": 0,
        "supplier_orders_created": 0,
        "orders_delivered": 0,
        "skipped": 0,
        "errors": 0,
        "provider_unavailable": 0,
        "details": [],
    }
    verification_payload = marketplace_client.verify_unique_code(unique_code)
    sale = _normalize_marketplace_sale(
        "digiseller",
        verification_payload,
        unique_code=unique_code,
    )
    _process_normalized_sale(
        db=db,
        sale=sale,
        supplier_client=supplier_client,
        marketplace_client=marketplace_client,
        stats=stats,
    )
    if stats["errors"] > 0:
        stats["status"] = "partial"
    if stats["orders_delivered"] == 0 and stats["errors"] == 0:
        stats["status"] = "pending"
    return stats


def _build_marketplace_clients(settings: Settings) -> dict[str, MarketplaceOrderClientProtocol]:
    clients: dict[str, MarketplaceOrderClientProtocol] = {}
    if settings.digiseller_api_key:
        clients["digiseller"] = DigisellerClient(
            settings.digiseller_base_url,
            settings.digiseller_api_key,
            seller_id=settings.digiseller_seller_id,
            task_poll_timeout_seconds=settings.digiseller_task_poll_timeout_seconds,
            task_poll_interval_seconds=settings.digiseller_task_poll_interval_seconds,
            sales_page_size=settings.digiseller_sales_page_size,
            sales_max_pages=settings.digiseller_sales_max_pages,
        )
    if settings.ggsel_api_key:
        clients["ggsel"] = GGSELClient(
            settings.ggsel_base_url,
            settings.ggsel_api_key,
            seller_id=settings.ggsel_seller_id,
        )
    return clients


def run_order_sync() -> dict[str, Any]:
    settings = get_settings()
    sync_run: SyncRun | None = None

    if not settings.gengine_api_key:
        log_event(
            logger,
            "order_sync.skipped",
            reason="gengine_api_key_missing",
            orders_detected=0,
            orders_processed=0,
        )
        return {
            "status": "skipped",
            "reason": "GENGINE_API_KEY is required for order sync.",
            "orders_detected": 0,
            "orders_processed": 0,
        }

    supplier_client = GEngineClient(settings.gengine_base_url, settings.gengine_api_key)
    marketplace_clients = _build_marketplace_clients(settings)
    try:
        with SessionLocal() as db:
            sync_run = SyncRun(job_name="order_sync", status="running")
            db.add(sync_run)
            db.commit()

            log_event(logger, "order_sync.started")
            stats = sync_orders(
                db=db,
                settings=settings,
                supplier_client=supplier_client,
                marketplace_clients=marketplace_clients,
            )

            sync_run.status = "success" if stats["status"] == "ok" else str(stats["status"])
            sync_run.finished_at = datetime.now(timezone.utc)
            sync_run.details_json = json.dumps(stats, ensure_ascii=True, separators=(",", ":"))
            db.add(sync_run)
            db.commit()

            log_event(
                logger,
                "order_sync.finished",
                orders_detected=stats["orders_detected"],
                orders_processed=stats["orders_processed"],
                orders_delivered=stats["orders_delivered"],
                skipped=stats["skipped"],
                errors=stats["errors"],
            )
            return stats
    except Exception as exc:
        with SessionLocal() as db:
            if sync_run is not None and sync_run.id is not None:
                run_row = db.get(SyncRun, sync_run.id)
                if run_row is not None:
                    run_row.status = "failed"
                    run_row.finished_at = datetime.now(timezone.utc)
                    run_row.error_message = str(exc)
                    db.add(run_row)
                    db.commit()
        log_event(logger, "order_sync.failed", error=str(exc))
        raise
    finally:
        for client in marketplace_clients.values():
            client.close()
        supplier_client.close()


def retry_order(order: Order, db: Session) -> dict[str, str]:
    action = get_retry_action(order.status, order.supplier_order_id)
    if action == "none":
        return {
            "status": "skipped",
            "reason": "Order is not in a retryable state.",
        }

    if action == "retry_delivery":
        if _supplier_order_can_be_recreated(order.error_message):
            order.supplier_order_id = None
            order.status = "pending"
            order.error_message = None
            db.add(order)
            db.commit()
            return {
                "status": "queued",
                "action": "retry_processing",
            }

        order.status = "supplier_order_created"
        order.error_message = None
        db.add(order)
        db.commit()
        return {
            "status": "queued",
            "action": "retry_delivery",
        }

    order.status = "pending"
    order.error_message = None
    db.add(order)
    db.commit()
    return {
        "status": "queued",
        "action": "retry_processing",
    }
