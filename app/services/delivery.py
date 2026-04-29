from __future__ import annotations

from typing import Any, Literal
from collections.abc import Sequence

from app.security import mask_gift_code

DeliveryProductKind = Literal["gift_card", "recharge", "steam_game", "product"]


def _normalize_codes(code: str | Sequence[str] | None) -> list[str]:
    if code is None:
        return []
    if isinstance(code, str):
        normalized = code.strip()
        return [normalized] if normalized else []
    codes: list[str] = []
    for item in code:
        normalized = str(item).strip()
        if normalized:
            codes.append(normalized)
    return codes


def detect_delivery_product_kind(
    *,
    supplier_product_id: str | None = None,
    product_title: str | None = None,
    product_category: str | None = None,
    product_payload: dict[str, Any] | None = None,
) -> DeliveryProductKind:
    payload = product_payload if isinstance(product_payload, dict) else {}
    supplier_id = str(supplier_product_id or "").strip().lower()
    source_type = str(payload.get("source_type") or "").strip().lower()
    recharge_product_id = str(payload.get("recharge_product_id") or "").strip()
    searchable = " ".join(
        str(value or "")
        for value in (
            product_title,
            product_category,
            payload.get("title"),
            payload.get("name"),
            payload.get("service_name"),
            payload.get("source_slug"),
        )
    ).lower()

    if (
        source_type == "recharge"
        or bool(recharge_product_id)
        or supplier_id.startswith("recharge:")
        or "recharge" in searchable
    ):
        return "recharge"
    if "gift card" in searchable or "gift-card" in searchable:
        return "gift_card"
    if source_type in {"gift", "gifts"} or supplier_id.startswith(("gift:", "gifts:")):
        return "steam_game"
    if "steam" in searchable and ("game" in searchable or "games" in searchable):
        return "steam_game"
    return "product"


def _product_lines(product_title: str | None) -> tuple[str, str]:
    title = str(product_title or "").strip()
    if not title:
        return "", ""
    return f"\nProduct: {title}", f"\nТовар: {title}"


def build_purchase_message(
    *,
    product_title: str | None = None,
    product_kind: DeliveryProductKind = "product",
) -> str:
    en_product, ru_product = _product_lines(product_title)
    if product_kind == "steam_game":
        return (
            "EN: Thank you for your purchase! Your order has been successfully processed. "
            f"Please wait while we prepare the delivery.{en_product}\n\n"
            "RU: Спасибо за вашу покупку! Ваш заказ успешно обработан. "
            f"Пожалуйста, подождите, пока мы подготовим доставку.{ru_product}"
        )
    return (
        "EN: Thank you for your purchase! Your order has been successfully processed "
        f"and will be delivered shortly.{en_product}\n\n"
        "RU: Спасибо за вашу покупку! Ваш заказ успешно обработан "
        f"и будет доставлен в ближайшее время.{ru_product}"
    )


def build_unique_code_request_message() -> str:
    return (
        "EN: Thank you for your purchase! To complete delivery, please paste your unique code "
        "(shown on your purchase page) in this chat.\n\n"
        "RU: Спасибо за вашу покупку! Для завершения доставки, пожалуйста, вставьте ваш уникальный код "
        "(указан на странице вашей покупки) в этот чат."
    )


def build_provider_unavailable_message(
    *,
    product_title: str | None = None,
) -> str:
    en_product, _ru_product = _product_lines(product_title)
    return (
        "EN: Our provider is currently down. We will deliver your order as soon as "
        f"it is active again.{en_product}"
    )


def _gift_card_delivery_message(codes: list[str], *, product_title: str | None) -> str:
    en_product, ru_product = _product_lines(product_title)
    if len(codes) == 1:
        en_code_block = f"Here is your code: {codes[0]}."
        ru_code_block = f"Вот ваш код: {codes[0]}."
    else:
        joined_codes = "\n".join(f"{index}. {item}" for index, item in enumerate(codes, start=1))
        en_code_block = f"Here are your codes:\n{joined_codes}"
        ru_code_block = f"Вот ваши коды:\n{joined_codes}"
    return (
        f"EN: Your gift card has been delivered.{en_product}\n"
        f"{en_code_block}\n"
        "Please leave a positive review if you are satisfied!\n\n"
        f"RU: Ваша подарочная карта доставлена.{ru_product}\n"
        f"{ru_code_block}\n"
        "Пожалуйста, оставьте положительный отзыв, если вы довольны!"
    )


def build_delivery_message(
    code: str | Sequence[str] | None,
    *,
    product_title: str | None = None,
    product_kind: DeliveryProductKind = "product",
) -> str:
    codes = _normalize_codes(code)
    if codes:
        return _gift_card_delivery_message(codes, product_title=product_title)

    en_product, ru_product = _product_lines(product_title)
    if product_kind == "steam_game":
        return (
            f"EN: Your game is ready to be delivered!{en_product}\n"
            "Please accept the friend request from our Steam account to receive your game. "
            "Don't forget to leave a positive review!\n\n"
            f"RU: Ваша игра готова к отправке!{ru_product}\n"
            "Пожалуйста, примите запрос в друзья от нашего аккаунта Steam, чтобы получить игру. "
            "Не забудьте оставить положительный отзыв!"
        )
    return (
        f"EN: Your product has been delivered successfully.{en_product}\n"
        "Please consider leaving a positive review!\n\n"
        f"RU: Ваш товар был успешно доставлен.{ru_product}\n"
        "Пожалуйста, оставьте положительный отзыв!"
    )


def masked_code_for_logs(code: str | None) -> str:
    return mask_gift_code(code)
