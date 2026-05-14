from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..security import new_id
from .base import Base, TimestampMixin


class Store(Base, TimestampMixin):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    exporter_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    exporter: Mapped["User"] = relationship("User", back_populates="stores")  # type: ignore[name-defined]
    market: Mapped["Market"] = relationship("Market")  # type: ignore[name-defined]
    products: Mapped[List["Product"]] = relationship("Product", back_populates="store")


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    exporter_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), nullable=False, index=True)

    product_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="NGN", nullable=False)
    images: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list (matches legacy contract)
    short_video_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min_order_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_order_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    properties: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    views: Mapped[int] = mapped_column(Integer, default=0)
    has_tax: Mapped[int] = mapped_column(Integer, default=0)
    is_featured: Mapped[int] = mapped_column(Integer, default=0)
    promote: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Inventory (added v2.5)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    last_inventory_update_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    exporter: Mapped["User"] = relationship("User", back_populates="products", foreign_keys=[exporter_id])  # type: ignore[name-defined]
    store: Mapped["Store"] = relationship("Store", back_populates="products")
    category: Mapped["Category"] = relationship("Category")  # type: ignore[name-defined]


class Cart(Base, TimestampMixin):
    __tablename__ = "carts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    importer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | ordered | abandoned

    items: Mapped[List["CartItem"]] = relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")


class CartItem(Base, TimestampMixin):
    __tablename__ = "cart_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="cartons")
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    cart: Mapped["Cart"] = relationship("Cart", back_populates="items")
    product: Mapped["Product"] = relationship("Product")
