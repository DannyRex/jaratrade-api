from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..security import new_id
from .base import Base, TimestampMixin


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_featured: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Market(Base, TimestampMixin):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    lga: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[str] = mapped_column(String(100), default="Nigeria", nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Bank(Base, TimestampMixin):
    __tablename__ = "banks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    country: Mapped[str] = mapped_column(String(100), default="Nigeria", nullable=False)
    paystack_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    flutter_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LogisticsCompany(Base, TimestampMixin):
    __tablename__ = "logistics_companies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LogisticsRate(Base, TimestampMixin):
    __tablename__ = "logistics_rates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    logistics_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    origin_country: Mapped[str] = mapped_column(String(100), default="Nigeria")
    destination_country: Mapped[str] = mapped_column(String(100), default="United Kingdom")
    base_rate: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    per_kg_rate: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="NGN")
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ImporterPlan(Base, TimestampMixin):
    __tablename__ = "importer_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    monthly_subscription_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    annual_subscription_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    transaction_limit: Mapped[float] = mapped_column(Numeric(14, 2), default=-1)  # -1 = unlimited
    commission_value: Mapped[float] = mapped_column(Numeric(12, 2), default=-1)
    commission_percent: Mapped[float] = mapped_column(Numeric(5, 2), default=2)
    product_limit: Mapped[int] = mapped_column(Integer, default=-1)
    currency: Mapped[str] = mapped_column(String(8), default="GBP")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ExporterPlan(Base, TimestampMixin):
    __tablename__ = "exporter_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    monthly_subscription_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    annual_subscription_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    transaction_limit: Mapped[float] = mapped_column(Numeric(14, 2), default=-1)
    commission_value: Mapped[float] = mapped_column(Numeric(12, 2), default=-1)
    commission_percent: Mapped[float] = mapped_column(Numeric(5, 2), default=2)
    product_promotion: Mapped[int] = mapped_column(Integer, default=0)
    max_product_promotion: Mapped[int] = mapped_column(Integer, default=0)
    max_market: Mapped[int] = mapped_column(Integer, default=-1)
    max_store: Mapped[int] = mapped_column(Integer, default=-1)
    max_store_per_market: Mapped[int] = mapped_column(Integer, default=-1)
    max_product_per_store: Mapped[int] = mapped_column(Integer, default=-1)
    max_product: Mapped[int] = mapped_column(Integer, default=-1)
    support_priority_level: Mapped[str] = mapped_column(String(8), default="3")
    currency: Mapped[str] = mapped_column(String(8), default="NGN")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
