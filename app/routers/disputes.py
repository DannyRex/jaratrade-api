"""Disputes / refunds.

Importer raises -> admin queue -> admin resolves (refund / replacement / dismissed).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import require_admin, require_exporter, require_importer
from ..envelope import fail, success
from ..models import Dispute, Order, Payment, Payout, Product, User
from ..services.email import (
    send_template,
    t_dispute_raised_buyer,
    t_dispute_raised_seller,
    t_dispute_resolved_buyer,
)
from ..services.flutterwave import refund_payment

settings = get_settings()

VALID_REASONS = {"damaged", "wrong_item", "not_received", "quality", "other"}
DISPUTE_WINDOW_DAYS = 7  # how long after delivery a buyer can raise a dispute


def _serialize(d: Dispute, order: Optional[Order] = None) -> dict:
    return {
        "id": d.id,
        "order_id": d.order_id,
        "order_number": order.order_number if order else None,
        "importer_id": d.importer_id,
        "exporter_id": d.exporter_id,
        "reason": d.reason,
        "description": d.description,
        "status": d.status,
        "resolution": d.resolution,
        "refund_amount": f"{float(d.refund_amount):.2f}" if d.refund_amount is not None else None,
        "refund_currency": d.refund_currency,
        "refund_tx_ref": d.refund_tx_ref,
        "replacement_order_id": d.replacement_order_id,
        "admin_notes": d.admin_notes,
        "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
        "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
        "time_created": d.time_created.isoformat(),
    }


# ───────────────────────── Importer ─────────────────────────

importer_router = APIRouter(prefix="/imp", tags=["disputes"])


@importer_router.post("/order/{order_id}/dispute")
def raise_dispute(
    order_id: str,
    reason: str = Form(...),
    description: str = Form(..., min_length=10, max_length=2000),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    if reason not in VALID_REASONS:
        raise fail(f"reason must be one of: {', '.join(sorted(VALID_REASONS))}")

    order = db.get(Order, order_id)
    if not order or order.importer_id != user.id:
        raise fail("Order not found", code=404)
    if order.status not in ("delivered", "shipped"):
        raise fail("Disputes can only be raised on shipped or delivered orders")

    # Time window
    if order.status == "delivered":
        elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - order.time_updated
        if elapsed > timedelta(days=DISPUTE_WINDOW_DAYS):
            raise fail(f"The {DISPUTE_WINDOW_DAYS}-day dispute window has closed for this order")

    # One open dispute per order
    existing = (
        db.query(Dispute)
        .filter(Dispute.order_id == order_id, Dispute.status.in_(["open", "in_review"]))
        .first()
    )
    if existing:
        raise fail("There's already an open dispute on this order", code=409)

    dispute = Dispute(
        order_id=order.id,
        importer_id=user.id,
        exporter_id=order.exporter_id,
        reason=reason,
        description=description,
        status="open",
    )
    db.add(dispute)
    db.commit()
    db.refresh(dispute)

    # Email both sides
    subject, html = t_dispute_raised_buyer(user.firstname or "there", order.order_number, reason)
    send_template(
        db, template="dispute_raised_buyer", to=user.email, subject=subject, html=html,
        user_id=user.id, dedupe_key=f"dispute_raised_buyer:{dispute.id}",
    )
    if order.exporter_id:
        exporter = db.get(User, order.exporter_id)
        if exporter:
            subject, html = t_dispute_raised_seller(
                exporter.firstname or "there", order.order_number, user.fullname or user.email, reason,
            )
            send_template(
                db, template="dispute_raised_seller", to=exporter.email, subject=subject, html=html,
                user_id=exporter.id, dedupe_key=f"dispute_raised_seller:{dispute.id}",
            )

    return success(_serialize(dispute, order))


@importer_router.get("/disputes")
def list_disputes(
    status: Optional[str] = Query(default=None, pattern="^(open|in_review|resolved|rejected)$"),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    q = db.query(Dispute).filter(Dispute.importer_id == user.id)
    if status:
        q = q.filter(Dispute.status == status)
    rows = q.order_by(desc(Dispute.time_created)).all()
    out = []
    for d in rows:
        order = db.get(Order, d.order_id)
        out.append(_serialize(d, order))
    return success({"rows": out, "total_length": len(out), "page": 0, "len": len(out)})


@importer_router.get("/disputes/{dispute_id}")
def get_dispute(
    dispute_id: str,
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    d = db.get(Dispute, dispute_id)
    if not d or d.importer_id != user.id:
        raise fail("Dispute not found", code=404)
    order = db.get(Order, d.order_id)
    return success(_serialize(d, order))


# ───────────────────────── Exporter ─────────────────────────
#
# Sellers need to know when buyers file complaints against them, even though
# they don't drive the resolution (admin does). Read-only views for their own
# disputes only.

exporter_router = APIRouter(prefix="/exp", tags=["exporter-disputes"])


@exporter_router.get("/disputes")
def list_disputes_exporter(
    status: Optional[str] = Query(default=None, pattern="^(open|in_review|resolved|rejected)$"),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    q = db.query(Dispute).filter(Dispute.exporter_id == user.id)
    if status:
        q = q.filter(Dispute.status == status)
    rows = q.order_by(desc(Dispute.time_created)).all()
    out = []
    for d in rows:
        order = db.get(Order, d.order_id)
        out.append(_serialize(d, order))
    return success({"rows": out, "total_length": len(out), "page": 0, "len": len(out)})


@exporter_router.get("/disputes/{dispute_id}")
def get_dispute_exporter(
    dispute_id: str,
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    d = db.get(Dispute, dispute_id)
    if not d or d.exporter_id != user.id:
        raise fail("Dispute not found", code=404)
    order = db.get(Order, d.order_id)
    return success(_serialize(d, order))


# ───────────────────────── Admin ─────────────────────────

admin_router = APIRouter(prefix="/adm/disputes", tags=["admin-disputes"])


@admin_router.get("")
def admin_list(
    status: Optional[str] = Query(default=None, pattern="^(open|in_review|resolved|rejected)$"),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=50, ge=1, le=200, alias="len"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(Dispute)
    if status:
        q = q.filter(Dispute.status == status)
    total = q.count()
    rows = q.order_by(desc(Dispute.time_created)).offset(p * len_).limit(len_).all()
    out = []
    for d in rows:
        order = db.get(Order, d.order_id)
        item = _serialize(d, order)
        importer = db.get(User, d.importer_id)
        item["importer_email"] = importer.email if importer else None
        item["importer_name"] = importer.fullname if importer else None
        out.append(item)

    # Counts across ALL disputes (independent of the status filter) so the
    # admin UI can show how many sit under each tab. Without this an
    # acknowledged ('in_review') dispute is easy to miss, since the page
    # lands on the 'open' tab by default.
    counts_raw = dict(
        db.query(Dispute.status, func.count(Dispute.id)).group_by(Dispute.status).all()
    )
    counts = {
        s: int(counts_raw.get(s, 0))
        for s in ("open", "in_review", "resolved", "rejected")
    }
    return success({
        "rows": out, "total_length": total, "page": p, "len": len_, "counts": counts,
    })


@admin_router.post("/{dispute_id}/acknowledge")
def acknowledge(dispute_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    d = db.get(Dispute, dispute_id)
    if not d:
        raise fail("Dispute not found", code=404)
    if d.status != "open":
        raise fail("Only open disputes can be acknowledged")
    d.status = "in_review"
    d.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return success(_serialize(d))


@admin_router.post("/{dispute_id}/resolve")
async def resolve(
    dispute_id: str,
    resolution: str = Form(...),  # refund | replacement | dismissed
    refund_amount: Optional[float] = Form(default=None),
    admin_notes: Optional[str] = Form(default=None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if resolution not in ("refund", "replacement", "dismissed"):
        raise fail("resolution must be 'refund', 'replacement' or 'dismissed'")

    d = db.get(Dispute, dispute_id)
    if not d:
        raise fail("Dispute not found", code=404)
    if d.status in ("resolved", "rejected"):
        raise fail("Dispute already closed", code=409)

    order = db.get(Order, d.order_id)
    if not order:
        raise fail("Underlying order not found", code=404)

    # Issue refund via Flutterwave if requested
    if resolution == "refund":
        payment = (
            db.query(Payment)
            .filter(Payment.order_id == d.order_id, Payment.status == "successful")
            .order_by(desc(Payment.time_created))
            .first()
        )
        if not payment:
            raise fail("No successful payment found for this order; cannot refund")
        # Money-safety guard: if the seller has already been paid out for this
        # order, refunding the buyer now would pay out twice. The payout has to
        # be reversed before a refund can be issued.
        already_paid_out = (
            db.query(Payout)
            .filter(
                Payout.order_id == d.order_id,
                Payout.status.in_(["sent", "completed"]),
            )
            .first()
        )
        if already_paid_out:
            raise fail(
                "The seller has already been paid out for this order - reverse "
                "the payout before issuing a refund.",
                code=409,
            )
        payload = json.loads(payment.provider_payload or "{}")
        flw_tx_id = str(payload.get("id") or payload.get("transaction_id") or "")
        if not flw_tx_id:
            raise fail("Original Flutterwave transaction ID not on record; cannot auto-refund. Issue manually and mark dismissed.")
        amount = refund_amount if refund_amount is not None else float(payment.amount)
        if amount <= 0:
            raise fail("Refund amount must be greater than zero")
        if amount > float(payment.amount):
            raise fail(
                f"Refund amount ({amount:.2f}) can't exceed what the buyer "
                f"paid ({float(payment.amount):.2f})"
            )
        try:
            flw = await refund_payment(flw_transaction_id=flw_tx_id, amount=amount)
        except Exception as e:  # noqa: BLE001
            raise fail(f"Flutterwave refund failed: {e!r}", code=502)
        d.refund_amount = amount
        d.refund_currency = payment.currency
        d.refund_tx_ref = flw.get("tx_ref") or payment.tx_ref
        d.refund_payload = json.dumps(flw)
        payment.status = "refunded"
        order.status = "refunded"
        # Restock items returned to the seller
        for item in order.items:
            prod = db.get(Product, item.product_id)
            if prod:
                prod.stock_quantity = (prod.stock_quantity or 0) + item.quantity

    d.status = "resolved"
    d.resolution = resolution
    d.admin_notes = admin_notes
    d.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if d.reviewed_at is None:
        d.reviewed_at = d.resolved_at
    db.commit()
    db.refresh(d)

    # Notify the buyer
    importer = db.get(User, d.importer_id)
    if importer:
        amount_str = (
            f"{float(d.refund_amount):.2f} {d.refund_currency}"
            if d.refund_amount is not None and d.refund_currency
            else ""
        )
        subject, html = t_dispute_resolved_buyer(
            importer.firstname or "there", order.order_number, resolution, amount_str,
        )
        send_template(
            db, template="dispute_resolved_buyer", to=importer.email, subject=subject, html=html,
            user_id=importer.id, dedupe_key=f"dispute_resolved:{d.id}",
        )
    return success(_serialize(d, order))


@admin_router.post("/{dispute_id}/reject")
def reject(
    dispute_id: str,
    admin_notes: str = Form(..., min_length=3),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    d = db.get(Dispute, dispute_id)
    if not d:
        raise fail("Dispute not found", code=404)
    if d.status in ("resolved", "rejected"):
        raise fail("Dispute already closed", code=409)
    d.status = "rejected"
    d.admin_notes = admin_notes
    d.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if d.reviewed_at is None:
        d.reviewed_at = d.resolved_at
    db.commit()

    importer = db.get(User, d.importer_id)
    order = db.get(Order, d.order_id)
    if importer and order:
        subject, html = t_dispute_resolved_buyer(
            importer.firstname or "there", order.order_number, "dismissed",
        )
        send_template(
            db, template="dispute_rejected", to=importer.email, subject=subject, html=html,
            user_id=importer.id, dedupe_key=f"dispute_rejected:{d.id}",
        )
    return success(_serialize(d, order))
