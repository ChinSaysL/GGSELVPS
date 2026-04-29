from __future__ import annotations

import re

from app.clients.base import APIRequestError

SUPPLIER_PROVIDER_UNAVAILABLE = "supplier_provider_unavailable"
SUPPLIER_PROVIDER_UNAVAILABLE_NOTIFIED = "supplier_provider_unavailable:buyer_notified"

_TEMPORARY_HTTP_STATUS_CODES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
}


def is_temporary_provider_error(exc: BaseException) -> bool:
    if not isinstance(exc, APIRequestError):
        return False

    message = str(exc)
    status_match = re.search(r"returned HTTP (\d{3})", message)
    if status_match is None:
        return True

    status_code = int(status_match.group(1))
    return status_code in _TEMPORARY_HTTP_STATUS_CODES or status_code >= 500
