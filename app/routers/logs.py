"""Logistics-partner facing endpoints (``/logs/orders``).

Logistics partners receive a signed link rather than maintaining accounts.
For now this is admin-gated; signed-link auth can be added later.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import Order, User

router = APIRouter(prefix="/logs", tags=["logistics-partner"])


@router.get("/orders")
def view_orders(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
):
    q = db.query(Order)
    if status:
        q = q.filter(Order.status == status)
    orders = q.order_by(desc(Order.time_created)).limit(100).all()
    rows = [{
        "id": o.id,
        "order_id": o.order_number,
        "status": o.status,
        "total": f"{float(o.total):.2f}",
        "currency": o.currency,
        "shipping_method": o.shipping_mode,
        "logistics_id": o.logistics_id,
        "time_created": o.time_created.isoformat(),
    } for o in orders]
    return success({"rows": rows, "total_length": len(rows), "page": 0, "len": len(rows)})


@router.post("/orders")
def update_order_status(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    order_id: str = Form(...),
    status: str = Form(...),
    notes: Optional[str] = Form(default=None),
):
    order = db.get(Order, order_id)
    if not order:
        raise fail("Order not found", code=404)
    order.status = status
    db.commit()
    return success({"status": status, "notes": notes})
