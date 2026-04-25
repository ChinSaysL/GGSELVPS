from __future__ import annotations

from app.security import mask_gift_code


def build_delivery_message(code: str) -> str:
    return (
        "Hello! Thank you for your purchase.\n\n"
        f"Your code is:\n{code}\n\n"
        "Please redeem it only on the correct region account. "
        "If you need help, message me here."
    )


def masked_code_for_logs(code: str | None) -> str:
    return mask_gift_code(code)
