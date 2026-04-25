from __future__ import annotations

from app.utils.logging import get_logger

logger = get_logger(__name__)


def create_supplier_order(*_: object, **__: object) -> dict[str, str]:
    # TODO: Wire to G-Engine client after endpoint details are finalized.
    raise NotImplementedError("Supplier order creation is not implemented yet.")
