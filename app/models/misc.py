from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..security import new_id
from .base import Base, TimestampMixin


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    firstname: Mapped[str] = mapped_column(String(100), nullable=False)
    lastname: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)


class Setting(Base, TimestampMixin):
    """Singleton key/value store for platform settings (commission account, etc.)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-serialized


class NotificationLog(Base, TimestampMixin):
    """Audit log of every transactional email we send.

    Useful for idempotency keys (e.g. don't re-send the same monthly-invoice
    email twice) and for support troubleshooting. The `dedupe_key` column lets
    callers pass a stable key like `invoice:user_id:2026-05` to skip duplicates.
    """

    __tablename__ = "notification_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    template: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    to_address: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
