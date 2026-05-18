from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..security import new_id
from .base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # importer | exporter | admin
    kind: Mapped[str] = mapped_column(String(16), default="individual", nullable=False)  # individual | business

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    firstname: Mapped[str] = mapped_column(String(100), nullable=False)
    middlename: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lastname: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    dob: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    profile_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ethnicity: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    valid_identification: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    passport: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 2FA - TOTP secret (RFC 6238). Stored base32-encoded.
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # KYC review state for exporters (pending | approved | rejected)
    kyc_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    kyc_reviewed_at: Mapped[Optional["datetime"]] = mapped_column(DateTime, nullable=True)
    kyc_rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Flutterwave subaccount the seller's share of each order's split lands in.
    # Populated by the KYC approval flow once the seller's bank details have been
    # validated against Flutterwave's resolve-account endpoint. Unverified
    # exporters or those with bad bank details have this null and can't transact.
    flw_subaccount_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    flw_subaccount_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plan_renewal_date: Mapped[Optional["datetime"]] = mapped_column(DateTime, nullable=True)
    plan_auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    product_delivered: Mapped[int] = mapped_column(Integer, default=0)
    monthly_spent: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    monthly_period_start: Mapped[Optional["datetime"]] = mapped_column(DateTime, nullable=True)

    business: Mapped[Optional["BusinessProfile"]] = relationship("BusinessProfile", uselist=False, back_populates="user", cascade="all, delete-orphan")
    stores: Mapped[List["Store"]] = relationship("Store", back_populates="exporter", cascade="all, delete-orphan")  # type: ignore[name-defined]
    products: Mapped[List["Product"]] = relationship("Product", back_populates="exporter", cascade="all, delete-orphan", foreign_keys="Product.exporter_id")  # type: ignore[name-defined]
    favourites: Mapped[List["FavouriteProduct"]] = relationship("FavouriteProduct", back_populates="user", cascade="all, delete-orphan")
    shipping_addresses: Mapped[List["ShippingAddress"]] = relationship("ShippingAddress", back_populates="user", cascade="all, delete-orphan")

    @property
    def fullname(self) -> str:
        parts = [self.firstname, self.middlename, self.lastname]
        return " ".join(p for p in parts if p)


class BusinessProfile(Base, TimestampMixin):
    __tablename__ = "business_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    business_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    business_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    business_reg_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    business_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    business_country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    annual_turnover: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    duration_in_business: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tin: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    valid_identification: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    documents: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list of doc URLs

    bank_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    account_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="business")


class EmailVerificationToken(Base, TimestampMixin):
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ShippingAddress(Base, TimestampMixin):
    __tablename__ = "shipping_addresses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False, default="United Kingdom")
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_default: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship("User", back_populates="shipping_addresses")


class FavouriteProduct(Base, TimestampMixin):
    __tablename__ = "favourite_products"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_favourite_user_product"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)

    user: Mapped["User"] = relationship("User", back_populates="favourites")
    product: Mapped["Product"] = relationship("Product")  # type: ignore[name-defined]
