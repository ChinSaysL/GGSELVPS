from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    gengine_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GENGINE_API_KEY", "GEngineToken"),
    )
    gengine_base_url: str = "https://api.g-engine.net"

    ggsel_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GGSEL_API_KEY", "GGSELToken"),
    )
    ggsel_base_url: str = "https://seller.ggsel.com"

    digiseller_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DIGISELLER_API_KEY", "DigisellerToken"),
    )
    digiseller_seller_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DIGISELLER_SELLER_ID", "DigisellerSellerId"),
    )
    digiseller_base_url: str = "https://api.digiseller.com"

    database_url: str = "sqlite:///./data/app.db"

    platform_fee_percent: float = 0.06
    fx_buffer_percent: float = 0.03
    risk_buffer_percent: float = 0.02
    safety_balance_percent: float = 0.15

    min_profit_cheap_usd: float = 0.50
    min_profit_medium_usd: float = 1.00
    min_profit_expensive_usd: float = 2.00

    manual_usd_rub_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("MANUAL_USD_RUB_RATE"),
    )
    auto_usd_rub_rate: bool = True
    usd_rub_rate_url: str = "https://www.cbr-xml-daily.ru/daily_json.js"
    usd_rub_rate_cache_path: str = "data/usd_rub_rate_cache.json"
    usd_rub_rate_refresh_hour_utc: int = 2
    default_currency: Literal["USD", "RUB"] = "RUB"

    order_poll_interval_seconds: int = 60
    product_sync_interval_minutes: int = 60
    stock_sync_interval_minutes: int = 5
    price_sync_interval_minutes: int = 60
    scheduler_enabled: bool = False

    log_level: str = "INFO"

    @field_validator("manual_usd_rub_rate", mode="before")
    @classmethod
    def empty_rate_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("digiseller_seller_id", mode="before")
    @classmethod
    def empty_seller_id_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "Settings":
        percent_fields = (
            "platform_fee_percent",
            "fx_buffer_percent",
            "risk_buffer_percent",
            "safety_balance_percent",
        )
        for field_name in percent_fields:
            value = float(getattr(self, field_name))
            if not 0 <= value < 1:
                msg = f"{field_name} must be within [0, 1)."
                raise ValueError(msg)

        interval_fields = (
            "order_poll_interval_seconds",
            "product_sync_interval_minutes",
            "stock_sync_interval_minutes",
            "price_sync_interval_minutes",
        )
        for field_name in interval_fields:
            value = int(getattr(self, field_name))
            if value <= 0:
                msg = f"{field_name} must be greater than 0."
                raise ValueError(msg)

        if self.manual_usd_rub_rate is not None and self.manual_usd_rub_rate <= 0:
            raise ValueError("MANUAL_USD_RUB_RATE must be greater than 0 when set.")

        if not 0 <= int(self.usd_rub_rate_refresh_hour_utc) <= 23:
            raise ValueError("usd_rub_rate_refresh_hour_utc must be within [0, 23].")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
