"""Admin payouts.

For v3.5 we offer the manual disbursement flow as the source of truth:
when an order is delivered + the 7-day dispute window has closed, admin
can release the seller's share to their bank account via Flutterwave's
transfers API. Future iterations will automate this via cron.

Routes:
  GET  /adm/payouts                  - list with status filter
  GET  /adm/payouts/eligible         - orders waiting for release
  POST /adm/payouts/{order_id}/send  - dispatch a payout

The seller share is computed at release time as
  order.total * (1 - commission_rate) - already_disputed_refund_amount
"""
from __future__ import annotations

import json
import secrets
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import Bank, Dispute, Order, Payment, Payout, User
from ..routers.settings_router import read_commission_rate
from ..services.flutterwave import FlutterwaveError, transfer_to_bank

router = APIRouter(prefix="/adm/payouts", tags=["admin-payouts"])

# How long after delivery before a payout can be released. Matches the
# dispute window so buyers always have a chance to raise an issue.
DISPUTE_WINDOW_DAYS = 7


def _serialize(p: Payout, order: Optional[Order] = None) -> dict:
    return {
        "id": p.id,
        "order_id": p.order_id,
        "order_number": order.order_number if order else None,
        "seller_id": p.seller_id,
        "amount": f"{float(p.amount):.2f}",
        "currency": p.currency,
        "reference": p.reference,
        "status": p.status,
        "failure_reason": p.failure_reason,
        "initiated_by": p.initiated_by,
        "time_created": p.time_created.isoformat(),
        "time_updated": p.time_updated.isoformat(),
    }


def _seller_share(order: Order, commission_rate_pct: float, db: Session) -> float:
    """How much the seller is owed for this order.

    = order.total * (1 - commission%)
      - any refund already disbursed via a resolved dispute
    """
    gross = float(order.total or 0)
    commission = gross * (commission_rate_pct / 100.0)
    refunded = 0.0
    for d in db.query(Dispute).filter(Dispute.order_id == order.id, Dispute.status == "resolved").all():
        if d.refund_amount:
            refunded += float(d.refund_amount)
    return max(0.0, gross - commission - refunded)


@router.get("")
def list_payouts(
    status: Optional[str] = Query(default=None, pattern="^(pending|sent|completed|failed)$"),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=50, ge=1, le=200, alias="len"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(Payout)
    if status:
        q = q.filter(Payout.status == status)
    total = q.count()
    rows = q.order_by(desc(Payout.time_created)).offset(p * len_).limit(len_).all()
    out = []
    for row in rows:
        order = db.get(Order, row.order_id)
        item = _serialize(row, order)
        seller = db.get(User, row.seller_id)
        item["seller_name"] = (seller.business.business_name if seller and seller.business else None) or (
            seller.fullname if seller else None
        )
        out.append(item)
    return success({"rows": out, "total_length": total, "page": p, "len": len_})


@router.get("/eligible")
def eligible_for_payout(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Orders that are delivered, past the dispute window, with no payout yet.

    Returns a payable preview - amount, seller, banking details, etc - so
    admin can review before dispatching.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DISPUTE_WINDOW_DAYS)
    delivered = (
        db.query(Order)
        .filter(Order.status == "delivered", Order.time_updated <= cutoff)
        .order_by(desc(Order.time_updated))
        .all()
    )
    # Filter out orders that already have a payout, or unpaid orders.
    rate_pct = read_commission_rate(db)
    preview = []
    for order in delivered:
        existing = db.query(Payout).filter(Payout.order_id == order.id).first()
        if existing:
            continue
        successful_payment = (
            db.query(Payment)
            .filter(Payment.order_id == order.id, Payment.status == "successful")
            .first()
        )
        if not successful_payment:
            continue
        seller = db.get(User, order.exporter_id) if order.exporter_id else None
        if not seller:
            continue
        bank = db.get(Bank, seller.business.bank_id) if seller.business and seller.business.bank_id else None
        share = _seller_share(order, rate_pct, db)
        preview.append({
            "order_id": order.id,
            "order_number": order.order_number,
            "delivered_at": order.time_updated.isoformat(),
            "gross_total": f"{float(order.total):.2f}",
            "commission_rate_percent": rate_pct,
            "seller_share": f"{share:.2f}",
            "currency": order.currency,
            "seller_id": seller.id,
            "seller_name": seller.business.business_name if seller.business else seller.fullname,
            "seller_bank": bank.name if bank else None,
            "seller_account_number": seller.business.account_number if seller.business else None,
            "flw_subaccount_id": seller.flw_subaccount_id,
            "bank_code": (bank.flutter_code or bank.paystack_code) if bank else None,
        })
    return success({"rows": preview, "total_length": len(preview), "page": 0, "len": len(preview)})


class PayoutDispatchError(Exception):
    """Raised when a payout can't be dispatched. Carries an HTTP-friendly code."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


async def dispatch_payout(
    order: Order,
    db: Session,
    initiated_by: Optional[str] = None,
) -> Payout:
    """Shared core: pre-flight checks + Flutterwave transfer + DB write.

    Used by both the admin HTTP endpoint (with an admin user id) and the
    nightly cron (with initiated_by=None / "cron"). Returns the persisted
    Payout row; raises PayoutDispatchError on any guard failure or FLW
    rejection so callers can map to the right HTTP status / log shape.
    """
    if order.status != "delivered":
        raise PayoutDispatchError(
            f"Order must be 'delivered' (currently '{order.status}')", code=400,
        )

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DISPUTE_WINDOW_DAYS)
    if order.time_updated > cutoff:
        raise PayoutDispatchError(
            "Dispute window still open. Earliest payout: "
            f"{(order.time_updated + timedelta(days=DISPUTE_WINDOW_DAYS)).strftime('%Y-%m-%d')}",
            code=400,
        )

    if db.query(Payout).filter(Payout.order_id == order.id).first():
        raise PayoutDispatchError("Payout already initiated for this order", code=409)

    successful = (
        db.query(Payment).filter(Payment.order_id == order.id, Payment.status == "successful").first()
    )
    if not successful:
        raise PayoutDispatchError("No successful payment on this order; nothing to pay out", code=400)

    seller = db.get(User, order.exporter_id) if order.exporter_id else None
    if not seller or not seller.business:
        raise PayoutDispatchError("Seller is missing a business profile", code=400)
    if not seller.business.account_number or not seller.business.bank_id:
        raise PayoutDispatchError("Seller's bank account isn't on file", code=400)
    bank = db.get(Bank, seller.business.bank_id)
    if not bank or not (bank.flutter_code or bank.paystack_code):
        raise PayoutDispatchError("Seller's bank has no Flutterwave code mapped", code=400)

    rate_pct = read_commission_rate(db)
    amount = _seller_share(order, rate_pct, db)
    if amount <= 0:
        raise PayoutDispatchError("Computed seller share is zero (full refund already issued?)", code=400)

    reference = "JARAPAY" + secrets.token_urlsafe(8).replace("-", "")[:10]
    payout = Payout(
        order_id=order.id,
        seller_id=seller.id,
        amount=amount,
        currency=order.currency,
        reference=reference,
        status="pending",
        initiated_by=initiated_by,
    )
    db.add(payout)
    db.commit()
    db.refresh(payout)

    try:
        resp = await transfer_to_bank(
            account_bank=bank.flutter_code or bank.paystack_code or "",
            account_number=seller.business.account_number,
            amount=amount,
            currency=order.currency,
            narration=f"Jaratrade payout: {order.order_number}",
            reference=reference,
            beneficiary_name=seller.business.business_name or seller.fullname,
        )
    except FlutterwaveError as e:
        payout.status = "failed"
        payout.failure_reason = f"{e.status_code}: {e.body}"
        db.commit()
        db.refresh(payout)
        raise PayoutDispatchError(
            f"Flutterwave rejected the transfer: {e.status_code} - {e.body}", code=502,
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        payout.status = "failed"
        payout.failure_reason = repr(e)
        db.commit()
        db.refresh(payout)
        raise PayoutDispatchError(f"Flutterwave call failed: {e!r}", code=502)

    payout.status = (
        "sent"
        if str(resp.get("status", "")).upper() in ("NEW", "PENDING", "QUEUED", "SUCCESSFUL", "COMPLETED")
        else "failed"
    )
    payout.provider_payload = json.dumps(resp)
    if payout.status == "failed":
        payout.failure_reason = json.dumps(resp)
    db.commit()
    db.refresh(payout)
    return payout


@router.post("/{order_id}/send")
async def send_payout(
    order_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Dispatch the seller payout for a delivered order."""
    order = db.get(Order, order_id)
    if not order:
        raise fail("Order not found", code=404)
    try:
        payout = await dispatch_payout(order, db, initiated_by=admin.id)
    except PayoutDispatchError as e:
        raise fail(str(e), code=e.code)
    return success(_serialize(payout, order))
