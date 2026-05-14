from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..security import new_id
from .base import Base, TimestampMixin


class Subscription(Base, TimestampMixin):
    """One row per subscription period (paid or pending) per user.

    Lifecycle:
      pending  -> upgrade endpoint created it, payment not yet verified
      active   -> payment verified; user is on this plan until period_end
      expired  -> period_end passed and not renewed (cron sets this)
      cancelled-> user cancelled before period_end (still active until period_end)
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_role: Mapped[str] = mapped_column(String(16), nullable=False)  # importer | exporter

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="GBP")

    tx_ref: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), default="flutterwave")
    provider_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Tokenized auto-recharge (v2.5): captured from Flutterwave on first verify.
    # On renewal, the recharge cron calls Flutterwave's `/tokenized-charges`
    # endpoint with this token rather than asking the user to enter card details again.
    flw_card_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    flw_card_last4: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    flw_card_brand: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_renewal_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    renewal_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
