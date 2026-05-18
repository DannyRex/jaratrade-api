from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..security import new_id
from .base import Base, TimestampMixin


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    cart_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    importer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    exporter_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    platform_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    logistics_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="NGN")

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    shipping_mode: Mapped[str] = mapped_column(String(20), default="logistics")  # self | logistics
    logistics_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    delivery_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # When the buyer explicitly confirmed receipt. Triggers immediate payout
    # eligibility (overrides the 7-day dispute-window wait).
    confirmed_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    items: Mapped[List["OrderItem"]] = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[List["Payment"]] = relationship("Payment", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    tx_ref: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="NGN")
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="flutterwave")
    provider_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="payments")


class Payout(Base, TimestampMixin):
    """Record of a seller payout against an order.

    States:
      - pending  : queued by admin, awaiting Flutterwave dispatch
      - sent     : POST /v3/transfers accepted; FLW will process T+0/T+1
      - completed: confirmed settled
      - failed   : FLW rejected or settlement bounced
    """
    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    seller_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="NGN")
    reference: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="flutterwave")
    provider_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    initiated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Review(Base, TimestampMixin):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    importer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    exporter_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
