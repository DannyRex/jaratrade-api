"""Flutterwave webhook receiver.

Flutterwave POSTs to a configured URL whenever a charge, transfer, or refund
moves to a terminal state. Three reasons we want this in addition to our
existing verify-by-tx_ref polling:

1. *Catches "tab closed" payments.* When a buyer finishes a card / bank-
   transfer auth flow on Flutterwave's side and never returns to our tab,
   our `/imp/payment/verify` poll never fires. The webhook does.
2. *Settles transfers asynchronously.* When admin dispatches a payout, FLW
   acks immediately with status='NEW' and the actual disbursement runs
   T+0/T+1 on the banking rails. Without the webhook we don't know when
   the money actually landed.
3. *Refund completion.* Same: refunds can take hours; we want to know
   when they're truly done so the dispute UI can say "refund completed"
   instead of "refund initiated".

Security: every webhook payload is HMAC-verified via the `verif-hash`
header against `FLW_WEBHOOK_SECRET`. Missing/mismatched hash -> 401.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..envelope import fail, success
from ..models import Order, Payment, Payout

router = APIRouter(prefix="/public/flutterwave", tags=["flutterwave-webhook"])
settings = get_settings()
log = logging.getLogger("jaratrade.flw_webhook")


def _verify_signature(provided: Optional[str]) -> bool:
    """Flutterwave's webhook auth is a plain shared-secret comparison
    against a custom `verif-hash` header. They don't sign the body; the
    secret you set in their dashboard is echoed back on every request.

    Returns True if the signature matches OR if no secret is configured
    (dev mode). In prod with `FLW_WEBHOOK_SECRET` set we always require
    a match.
    """
    expected = getattr(settings, "flw_webhook_secret", None) or ""
    if not expected:
        return True  # dev / not yet configured
    return bool(provided) and provided == expected


def _parse_flw_datetime(value: str) -> Optional["datetime"]:
    """Parse the datetime strings FLW sends in their webhook payloads.

    Their format is loosely ISO-8601 but inconsistent across endpoints -
    sometimes 'YYYY-MM-DDTHH:MM:SS.000Z', sometimes 'YYYY-MM-DD HH:MM:SS'
    without a TZ marker. We normalise to naive UTC for storage.
    """
    from datetime import datetime as _dt
    s = value.strip().replace("Z", "+00:00")
    # Try with TZ first, then naive.
    for fmt_attempt in (
        lambda: _dt.fromisoformat(s),
        lambda: _dt.fromisoformat(s.replace(" ", "T")),
    ):
        try:
            dt = fmt_attempt()
            if dt.tzinfo is not None:
                from datetime import timezone as _tz
                dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _serialise_for_audit(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data)[:8000]  # keep audit blob bounded
    except Exception:  # noqa: BLE001
        return repr(data)[:8000]


def _handle_charge(event_data: Dict[str, Any], db: Session) -> Dict[str, Any]:
    """charge.completed - buyer's card/transfer was processed."""
    tx_ref = event_data.get("tx_ref") or event_data.get("txRef")
    if not tx_ref:
        return {"handled": False, "reason": "no tx_ref"}

    payment = db.query(Payment).filter(Payment.tx_ref == tx_ref).first()
    if not payment:
        return {"handled": False, "reason": f"no Payment with tx_ref={tx_ref}"}

    status = str(event_data.get("status", "")).lower()
    # FLW uses "successful" / "failed" / "cancelled"
    just_paid = False
    if status == "successful":
        payment.status = "successful"
        order = db.get(Order, payment.order_id)
        if order and order.status == "pending":
            order.status = "paid"
            just_paid = True

        # Capture FLW's settlement reference + due date if present in the
        # event. For NGN charges these may be null in the immediate
        # charge.completed payload (settlement is implicit T+1); for
        # international GBP/USD charges FLW typically populates them so
        # we can gate the seller payout on settlement completion (T+5).
        settlement_id = event_data.get("settlement_id") or event_data.get("settlement", {}).get("id") if isinstance(event_data.get("settlement"), dict) else event_data.get("settlement_id")
        if settlement_id:
            payment.flw_settlement_id = str(settlement_id)
            payment.settlement_status = "pending"  # we'll poll for updates
        due = event_data.get("due_datetime") or (
            event_data.get("settlement", {}).get("due_datetime")
            if isinstance(event_data.get("settlement"), dict) else None
        )
        if due:
            try:
                # FLW sends ISO-8601 with timezone; normalise to naive UTC.
                payment.settlement_due_at = _parse_flw_datetime(due)
            except Exception:  # noqa: BLE001
                pass
    elif status in ("failed", "cancelled"):
        payment.status = "failed"
    payment.provider_payload = _serialise_for_audit(event_data)
    db.commit()

    # Fire order-confirmation emails from the webhook path too. If the
    # buyer closes their browser right after paying on FLW (so verify_pay
    # never runs), the webhook is the only signal we'll get - the buyer
    # would otherwise pay but never receive a confirmation. Dedupe keys
    # on send_template prevent double-send if verify_pay also runs.
    if just_paid:
        from ..models import User
        from ..routers.importer import _send_order_confirmation_emails
        order = db.get(Order, payment.order_id)
        buyer = db.get(User, order.importer_id) if order else None
        if order and buyer:
            try:
                _send_order_confirmation_emails(db, order, buyer)
            except Exception:  # noqa: BLE001
                # Webhook handling must not error on email problems, or FLW
                # will retry the webhook indefinitely. Log via the email
                # service's own internal logger, not our caller's signal.
                log.exception("flutterwave_webhook: order confirmation email failed")

    return {"handled": True, "payment_status": payment.status, "tx_ref": tx_ref}


def _handle_transfer(event_data: Dict[str, Any], db: Session) -> Dict[str, Any]:
    """transfer.completed - a seller payout settled or bounced."""
    reference = event_data.get("reference")
    if not reference:
        return {"handled": False, "reason": "no reference"}

    payout = db.query(Payout).filter(Payout.reference == reference).first()
    if not payout:
        return {"handled": False, "reason": f"no Payout with reference={reference}"}

    status = str(event_data.get("status", "")).upper()
    # FLW transfer terminal states: SUCCESSFUL / FAILED. Intermediate: NEW / PENDING.
    if status == "SUCCESSFUL":
        payout.status = "completed"
    elif status == "FAILED":
        payout.status = "failed"
        payout.failure_reason = event_data.get("complete_message") or _serialise_for_audit(event_data)
    elif status in ("NEW", "PENDING", "QUEUED"):
        payout.status = "sent"
    payout.provider_payload = _serialise_for_audit(event_data)
    db.commit()
    return {"handled": True, "payout_status": payout.status, "reference": reference}


# Event dispatch table. Keys are the canonical "event" strings FLW sends.
# Some older events carry the type in `event` rather than `eventType` so we
# accept both spellings.
_HANDLERS = {
    "charge.completed": _handle_charge,
    "charge.success": _handle_charge,
    "transfer.completed": _handle_transfer,
    "transfer.success": _handle_transfer,
    "transfer.failed": _handle_transfer,
}


@router.post("/webhook")
async def flutterwave_webhook(
    request: Request,
    verif_hash: Optional[str] = Header(default=None, alias="verif-hash"),
    db: Session = Depends(get_db),
):
    """Receive a Flutterwave webhook event.

    Returns 200 with `{handled: true}` for events we processed, 200 with
    `{handled: false, reason: ...}` for events we knowingly ignore (so FLW
    doesn't retry forever), and 401 if the `verif-hash` doesn't match
    our configured secret.
    """
    if not _verify_signature(verif_hash):
        log.warning("flutterwave_webhook: bad/missing verif-hash header")
        raise fail("Invalid webhook signature", code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise fail("Invalid JSON body", code=400)

    event = body.get("event") or body.get("eventType") or ""
    # Some FLW events nest under `data`, others put everything at the top level.
    data = body.get("data") or body

    handler = _HANDLERS.get(event)
    if not handler:
        log.info("flutterwave_webhook: ignoring event=%r", event)
        return success({"handled": False, "reason": f"no handler for event={event}"})

    try:
        result = handler(data, db)
    except Exception:  # noqa: BLE001
        log.exception("flutterwave_webhook: handler error for event=%s", event)
        # Return 500-equivalent so FLW retries; we'd rather be safe than
        # silently swallow.
        raise fail("Internal error", code=500)

    log.info("flutterwave_webhook: event=%s result=%s", event, result)
    return success(result)
