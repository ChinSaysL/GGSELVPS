from __future__ import annotations

from collections.abc import Sequence

from app.security import mask_gift_code


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


def build_delivery_message(
    code: str | Sequence[str] | None,
    *,
    product_title: str | None = None,
) -> str:
    codes = _normalize_codes(code)
    title_line = f" for {product_title}" if product_title else ""
    if not codes:
        return (
            "Hello! Thank you for your purchase.\n\n"
            f"Your order{title_line} has been fulfilled.\n\n"
            "If you need help, message me here."
        )
    if len(codes) == 1:
        code_block = f"Your code is:\n{codes[0]}"
    else:
        joined_codes = "\n".join(f"{index}. {item}" for index, item in enumerate(codes, start=1))
        code_block = f"Your codes are:\n{joined_codes}"
    return (
        "Hello! Thank you for your purchase.\n\n"
        f"{code_block}\n\n"
        "Please redeem it only on the correct region account. "
        "If you need help, message me here."
    )


def masked_code_for_logs(code: str | None) -> str:
    return mask_gift_code(code)
