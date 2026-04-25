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
    digiseller_base_url: str = "https://api.digiseller.com"
    digiseller_category_id: int | None = None
    digiseller_category_owner: int = 3
    digiseller_content_type: Literal["Text", "File", "Url", "DigisellerCode", "Form"] = "Text"

    listing_title_template_en: str = (
        "[AUTO 24/7] Steam Gift Card {denomination} {currency} | {region}"
    )
    listing_title_template_ru: str = (
        "[AUTO 24/7] Steam Gift Card {denomination} {currency} | {region}"
    )
    listing_description_template_en: str = (
        "Instant digital delivery.\n"
        "Product: {supplier_title}\n"
        "Region: {region}\n"
        "Denomination: {denomination} {currency}\n"
        "Delivery format: {delivery_type}\n"
        "Support: contact us in chat if you need help."
    )
    listing_description_template_ru: str = (
        "Мгновенная цифровая доставка.\n"
        "Товар: {supplier_title}\n"
        "Регион: {region}\n"
        "Номинал: {denomination} {currency}\n"
        "Формат выдачи: {delivery_type_ru}\n"
        "Поддержка: если нужна помощь, напишите нам в чат."
    )

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
    default_currency: Literal["USD", "RUB"] = "RUB"

    order_poll_interval_seconds: int = 60
    product_sync_interval_minutes: int = 60
    stock_sync_interval_minutes: int = 5
    price_sync_interval_minutes: int = 30
    scheduler_enabled: bool = True

    log_level: str = "INFO"

    @field_validator("manual_usd_rub_rate", mode="before")
    @classmethod
    def empty_rate_to_none(cls, value: object) -> object:
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

        if self.digiseller_category_id is not None and self.digiseller_category_id <= 0:
            raise ValueError("DIGISELLER_CATEGORY_ID must be greater than 0 when set.")

        if self.digiseller_category_owner not in {0, 1, 2, 3}:
            raise ValueError("DIGISELLER_CATEGORY_OWNER must be one of: 0, 1, 2, 3.")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
