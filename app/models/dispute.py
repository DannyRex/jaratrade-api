from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..security import new_id
from .base import Base, TimestampMixin


class Dispute(Base, TimestampMixin):
    """Importer-raised issue on a delivered order.

    Lifecycle:
        open       -> importer just raised, admin queue
        in_review  -> admin has acknowledged
        resolved   -> admin granted resolution (refund / replacement / dismissed)
        rejected   -> admin closed without acting

    Resolution is recorded separately so we can render rich UI:
        refund      -> Flutterwave refund issued for `refund_amount`
        replacement -> new order created (link via `replacement_order_id`)
        dismissed   -> no monetary action
    """

    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    importer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    exporter_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    reason: Mapped[str] = mapped_column(String(80), nullable=False)  # damaged | wrong_item | not_received | quality | other
    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # refund | replacement | dismissed

    refund_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    refund_currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    refund_tx_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    refund_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON from Flutterwave

    replacement_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    admin_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
