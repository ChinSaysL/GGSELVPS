from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.pricing import PricingConfigError


@dataclass(frozen=True)
class FxRateSnapshot:
    rate: Decimal
    fetched_at: datetime
    source: str


def _cache_path(settings: Settings) -> Path:
    path = Path(settings.usd_rub_rate_cache_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_snapshot(path: Path) -> FxRateSnapshot | None:
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    raw_rate = payload.get("rate")
    raw_fetched_at = payload.get("fetched_at")
    raw_source = payload.get("source")
    if raw_rate is None or raw_fetched_at is None or raw_source is None:
        return None

    try:
        rate = Decimal(str(raw_rate))
        fetched_at = datetime.fromisoformat(str(raw_fetched_at))
    except Exception:
        return None

    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    if rate <= 0:
        return None

    return FxRateSnapshot(rate=rate, fetched_at=fetched_at, source=str(raw_source))


def _save_snapshot(path: Path, snapshot: FxRateSnapshot) -> None:
    payload = {
        "rate": str(snapshot.rate),
        "fetched_at": snapshot.fetched_at.astimezone(timezone.utc).isoformat(),
        "source": snapshot.source,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")


def _extract_usd_rub_rate(payload: dict[str, Any]) -> Decimal:
    valute = payload.get("Valute")
    if not isinstance(valute, dict):
        raise PricingConfigError("FX feed payload is missing Valute data.")

    usd = valute.get("USD")
    if not isinstance(usd, dict):
        raise PricingConfigError("FX feed payload is missing USD data.")

    value = usd.get("Value")
    if value is None:
        raise PricingConfigError("FX feed payload is missing USD Value.")

    try:
        rate = Decimal(str(value))
    except Exception as exc:
        raise PricingConfigError("FX feed returned invalid USD Value.") from exc

    if rate <= 0:
        raise PricingConfigError("FX feed returned non-positive USD/RUB rate.")

    return rate


def _fetch_usd_rub_rate(settings: Settings) -> FxRateSnapshot:
    try:
        response = httpx.get(settings.usd_rub_rate_url, timeout=15.0)
    except httpx.RequestError as exc:
        raise PricingConfigError("USD/RUB rate request failed.") from exc

    if response.is_error:
        raise PricingConfigError(f"USD/RUB rate request failed with HTTP {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PricingConfigError("USD/RUB rate response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise PricingConfigError("USD/RUB rate response has unexpected format.")

    rate = _extract_usd_rub_rate(payload)
    return FxRateSnapshot(
        rate=rate,
        fetched_at=datetime.now(timezone.utc),
        source=settings.usd_rub_rate_url,
    )


def refresh_usd_rub_rate(settings: Settings, *, force: bool = False) -> FxRateSnapshot:
    cache_file = _cache_path(settings)
    cached = _load_snapshot(cache_file)
    today_utc = datetime.now(timezone.utc).date()

    if not force and cached is not None and cached.fetched_at.astimezone(timezone.utc).date() == today_utc:
        return cached

    try:
        fresh = _fetch_usd_rub_rate(settings)
    except PricingConfigError:
        if cached is not None:
            return cached
        raise

    _save_snapshot(cache_file, fresh)
    return fresh


def resolve_usd_rub_rate(settings: Settings) -> Decimal:
    if settings.manual_usd_rub_rate is not None:
        return Decimal(str(settings.manual_usd_rub_rate))

    if not settings.auto_usd_rub_rate:
        raise PricingConfigError("MANUAL_USD_RUB_RATE is required when automatic FX is disabled.")

    snapshot = refresh_usd_rub_rate(settings)
    return snapshot.rate
