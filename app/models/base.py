from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base  # re-export


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    time_created: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    time_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
