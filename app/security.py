from __future__ import annotations


def mask_secret(value: str | None, visible_chars: int = 4) -> str:
    if not value:
        return ""
    visible = max(0, visible_chars)
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def mask_gift_code(code: str | None) -> str:
    if not code:
        return ""
    normalized = code.strip()
    if len(normalized) <= 4:
        return "*" * len(normalized)
    return f"{normalized[:2]}{'*' * (len(normalized) - 4)}{normalized[-2:]}"
