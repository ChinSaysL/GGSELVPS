from __future__ import annotations

from typing import Any, cast

import httpx

from app.security import mask_secret


class APIClientError(RuntimeError):
    pass


class APIClientConfigError(APIClientError):
    pass


class APIRequestError(APIClientError):
    pass


class BaseAPIClient:
    def __init__(
        self,
        *,
        service_name: str,
        base_url: str,
        api_key: str | None,
        timeout: float = 20.0,
    ) -> None:
        self.service_name = service_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BaseAPIClient":
        return self

    def __exit__(self, _: object, __: object, ___: object) -> None:
        self.close()

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise APIClientConfigError(
                f"{self.service_name} API key is missing. "
                f"Current key: {mask_secret(self.api_key)}"
            )
        return self.api_key

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        try:
            response = self.http.request(
                method=method.upper(),
                url=path,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.RequestError as exc:
            raise APIRequestError(
                f"{self.service_name} request failed: {method.upper()} {path}"
            ) from exc

        if response.is_error:
            raise APIRequestError(
                f"{self.service_name} returned HTTP {response.status_code} for "
                f"{method.upper()} {path}"
            )

        try:
            return cast(dict[str, Any] | list[Any], response.json())
        except ValueError as exc:
            raise APIRequestError(
                f"{self.service_name} returned non-JSON response for {method.upper()} {path}"
            ) from exc
