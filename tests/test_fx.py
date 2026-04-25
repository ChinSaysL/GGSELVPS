from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.services import fx


def _settings(cache_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        scheduler_enabled=False,
        usd_rub_rate_cache_path=str(cache_path),
    )


def test_resolve_usd_rub_rate_uses_manual_override(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        scheduler_enabled=False,
        MANUAL_USD_RUB_RATE=99.5,
        usd_rub_rate_cache_path=str(tmp_path / "rate.json"),
    )

    assert fx.resolve_usd_rub_rate(settings) == Decimal("99.5")


def test_refresh_usd_rub_rate_uses_today_cache(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path / "rate.json")
    now = datetime.now(timezone.utc)
    cached = fx.FxRateSnapshot(rate=Decimal("90.12"), fetched_at=now, source="cache")
    fx._save_snapshot(Path(settings.usd_rub_rate_cache_path), cached)

    calls = {"count": 0}

    def fake_fetch(_settings: Settings) -> fx.FxRateSnapshot:
        calls["count"] += 1
        return fx.FxRateSnapshot(rate=Decimal("91.00"), fetched_at=now, source="remote")

    monkeypatch.setattr(fx, "_fetch_usd_rub_rate", fake_fetch)

    snapshot = fx.refresh_usd_rub_rate(settings)

    assert snapshot.rate == Decimal("90.12")
    assert calls["count"] == 0


def test_refresh_usd_rub_rate_fetches_when_cache_stale(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path / "rate.json")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    stale = fx.FxRateSnapshot(rate=Decimal("90.12"), fetched_at=yesterday, source="cache")
    fx._save_snapshot(Path(settings.usd_rub_rate_cache_path), stale)

    def fake_fetch(_settings: Settings) -> fx.FxRateSnapshot:
        return fx.FxRateSnapshot(
            rate=Decimal("91.00"),
            fetched_at=datetime.now(timezone.utc),
            source="remote",
        )

    monkeypatch.setattr(fx, "_fetch_usd_rub_rate", fake_fetch)

    snapshot = fx.refresh_usd_rub_rate(settings)

    assert snapshot.rate == Decimal("91.00")
