from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SupplierProduct(Base):
    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint("supplier", "supplier_product_id", name="uq_supplier_product_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier: Mapped[str] = mapped_column(String(50), index=True)
    supplier_product_id: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    denomination: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    supplier_stock: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    offers: Mapped[list["MarketplaceOffer"]] = relationship(back_populates="supplier_product")
    orders: Mapped[list["Order"]] = relationship(back_populates="supplier_product")


class MarketplaceOffer(Base):
    __tablename__ = "marketplace_offers"
    __table_args__ = (
        UniqueConstraint("marketplace", "marketplace_offer_id", name="uq_marketplace_offer_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    marketplace: Mapped[str] = mapped_column(String(50), index=True)
    marketplace_offer_id: Mapped[str] = mapped_column(String(120), index=True)
    supplier_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_products.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(10), default="RUB", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manually_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_buyer_input: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    buyer_input_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_input_help: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    supplier_product: Mapped[SupplierProduct | None] = relationship(back_populates="offers")
    mappings: Mapped[list["ProductMapping"]] = relationship(back_populates="marketplace_offer")
    orders: Mapped[list["Order"]] = relationship(back_populates="marketplace_offer")


class ProductMapping(Base):
    __tablename__ = "product_mappings"
    __table_args__ = (
        UniqueConstraint(
            "supplier_product_id",
            "marketplace_offer_id",
            name="uq_supplier_offer_mapping_pair",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_product_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_products.id"), nullable=False, index=True
    )
    marketplace_offer_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_offers.id"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    marketplace_variant_id: Mapped[int | None] = mapped_column(nullable=True)
    marketplace_edition_variant_id: Mapped[int | None] = mapped_column(nullable=True)
    marketplace_variant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marketplace_commission_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    payment_system_commission_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4),
        nullable=True,
    )
    min_margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    min_profit_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    manage_stock_quantity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    min_purchase_quantity: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    supplier_product: Mapped[SupplierProduct] = relationship()
    marketplace_offer: Mapped[MarketplaceOffer] = relationship(back_populates="mappings")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("marketplace", "marketplace_order_id", name="uq_marketplace_order_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    marketplace: Mapped[str] = mapped_column(String(50), index=True)
    marketplace_order_id: Mapped[str] = mapped_column(String(120), index=True)
    marketplace_offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_offers.id"), nullable=True
    )
    supplier_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_products.id"), nullable=True
    )
    buyer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True, nullable=False)
    supplier_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    delivered_code_encrypted_or_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    marketplace_offer: Mapped[MarketplaceOffer | None] = relationship(back_populates="orders")
    supplier_product: Mapped[SupplierProduct | None] = relationship(back_populates="orders")


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier: Mapped[str] = mapped_column(String(50), index=True)
    balance_usd: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    usable_balance_usd: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
