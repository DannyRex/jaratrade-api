"""Subscription management - upgrade, verify, cancel, fetch current.

Flow (per role):
  1. POST /imp/subscription/upgrade  body: {plan_id}
        -> creates a `pending` Subscription
        -> returns Flutterwave Inline-checkout config
  2. Frontend launches Flutterwave inline; on success calls verify
  3. POST /imp/subscription/verify   body: {tx_ref}
        -> verifies with Flutterwave (or dev fallback)
        -> activates the subscription, sets user.plan_id + plan_renewal_date
        -> emails the user a confirmation
  4. POST /imp/subscription/cancel
        -> stops auto-renew. User keeps premium until period_end.
  5. GET  /imp/subscription
        -> current subscription + plan details

Same surface mirrored at /exp/* for exporters. Same Subscription model handles both.

Renewal: nightly cron (`app/cron.py`) downgrades expired plans + emails.
Future: tokenize the first payment so cron can charge it again automatically.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..config import get_settings
from ..constants import ROLE_EXPORTER, ROLE_IMPORTER
from ..database import get_db
from ..deps import require_exporter, require_importer
from ..envelope import fail, success
from ..models import ExporterPlan, ImporterPlan, Subscription, User
from ..rate_limit import limiter
from ..services.email import (
    send_template,
    t_subscription_cancelled,
    t_subscription_payment_confirmed,
)
from ..services.flutterwave import build_inline_config, verify_payment

settings = get_settings()
SUBSCRIPTION_PERIOD_DAYS = 30


# ───────────────────────── Plan helpers ─────────────────────────

def _get_plan(db: Session, plan_id: str, role: str):
    Model = ImporterPlan if role == ROLE_IMPORTER else ExporterPlan
    return db.get(Model, plan_id)


def _serialize_subscription(sub: Subscription, plan_title: Optional[str] = None) -> dict:
    return {
        "id": sub.id,
        "user_id": sub.user_id,
        "plan_id": sub.plan_id,
        "plan_role": sub.plan_role,
        "plan_title": plan_title,
        "status": sub.status,
        "period_start": sub.period_start.isoformat() if sub.period_start else None,
        "period_end": sub.period_end.isoformat() if sub.period_end else None,
        "amount": f"{float(sub.amount):.2f}",
        "currency": sub.currency,
        "tx_ref": sub.tx_ref,
        "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
        "time_created": sub.time_created.isoformat(),
        # Surface stored-card metadata (not the token itself) so the UI can show
        # "Visa •4242" and let the user know auto-renew is wired up.
        "card_last4": sub.flw_card_last4,
        "card_brand": sub.flw_card_brand,
        "has_payment_token": bool(sub.flw_card_token),
        "renewal_failure_count": sub.renewal_failure_count or 0,
        "last_renewal_attempt_at": sub.last_renewal_attempt_at.isoformat() if sub.last_renewal_attempt_at else None,
    }


def _current_subscription(db: Session, user_id: str) -> Optional[Subscription]:
    return (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id, Subscription.status.in_(["active", "cancelled"]))
        .order_by(desc(Subscription.time_created))
        .first()
    )


# ───────────────────────── Core handlers ─────────────────────────

def _upgrade(db: Session, user: User, role: str, plan_id: str):
    plan = _get_plan(db, plan_id, role)
    if not plan:
        raise fail("Plan not found", code=404)
    fee = float(plan.monthly_subscription_fee or 0)
    if fee <= 0:
        # Switching to a free plan: no payment needed, just update plan_id immediately.
        user.plan_id = plan.id
        user.plan_renewal_date = None
        user.plan_auto_renew = True
        db.commit()
        return success({"requires_payment": False, "plan_id": plan.id, "plan_title": plan.title})

    tx_ref = "JARASUB" + secrets.token_urlsafe(8).replace("-", "")[:10]
    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        plan_role=role,
        status="pending",
        amount=fee,
        currency=plan.currency,
        tx_ref=tx_ref,
    )
    db.add(sub)
    db.commit()

    config = build_inline_config(
        tx_ref=tx_ref,
        amount=fee,
        currency=plan.currency,
        customer={"email": user.email, "phone_number": user.phone or "", "name": user.fullname},
        order_id=f"SUB-{plan.title}",
    )
    config["meta"] = {
        "type": "subscription",
        "subscription_id": sub.id,
        "plan_id": plan.id,
        "plan_role": role,
    }
    return success({"requires_payment": True, "subscription_id": sub.id, **config})


async def _verify(db: Session, user: User, tx_ref: str):
    sub = db.query(Subscription).filter(Subscription.tx_ref == tx_ref).first()
    if not sub or sub.user_id != user.id:
        raise fail("Subscription not found", code=404)
    if sub.status == "active":
        return success(_serialize_subscription(sub), message="Already active")

    try:
        flw = await verify_payment(tx_ref)
    except Exception as e:  # noqa: BLE001
        sub.provider_payload = json.dumps({"error": repr(e)})
        db.commit()
        raise fail("Payment verification failed", code=502)

    if not flw or flw.get("status") != "successful":
        sub.status = "expired"
        sub.provider_payload = json.dumps(flw or {})
        db.commit()
        raise fail("Payment was not successful", code=402)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sub.status = "active"
    sub.period_start = now
    sub.period_end = now + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
    sub.provider_payload = json.dumps(flw)

    # Capture card token so the renewal cron can charge again without the user.
    # Flutterwave returns `card: {token, last_4digits, type, ...}` on card txns.
    card = (flw or {}).get("card") or {}
    if isinstance(card, dict) and card.get("token"):
        sub.flw_card_token = card.get("token")
        sub.flw_card_last4 = card.get("last_4digits") or card.get("last4")
        sub.flw_card_brand = card.get("type") or card.get("brand")
        sub.renewal_failure_count = 0

    plan = _get_plan(db, sub.plan_id, sub.plan_role)
    user.plan_id = sub.plan_id
    user.plan_renewal_date = sub.period_end
    user.plan_auto_renew = True
    db.commit()
    db.refresh(sub)

    if plan:
        subject, html = t_subscription_payment_confirmed(
            user.firstname or "there",
            plan.title,
            f"{float(sub.amount):.2f} {sub.currency}",
            sub.period_end.strftime("%d %b %Y"),
        )
        send_template(
            db, template="subscription_payment_confirmed",
            to=user.email, subject=subject, html=html, user_id=user.id,
            dedupe_key=f"sub_paid:{sub.id}",
        )

    return success(_serialize_subscription(sub, plan.title if plan else None))


def _cancel(db: Session, user: User):
    sub = _current_subscription(db, user.id)
    if not sub or sub.status != "active":
        raise fail("No active subscription to cancel", code=404)
    sub.status = "cancelled"
    sub.cancelled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    user.plan_auto_renew = False
    db.commit()
    plan = _get_plan(db, sub.plan_id, sub.plan_role)
    if plan and sub.period_end:
        subject, html = t_subscription_cancelled(
            user.firstname or "there", plan.title, sub.period_end.strftime("%d %b %Y"),
        )
        send_template(
            db, template="subscription_cancelled",
            to=user.email, subject=subject, html=html, user_id=user.id,
            dedupe_key=f"sub_cancelled:{sub.id}",
        )
    return success(_serialize_subscription(sub, plan.title if plan else None))


def _current(db: Session, user: User, role: str):
    sub = _current_subscription(db, user.id)
    plan = None
    if user.plan_id:
        plan = _get_plan(db, user.plan_id, role)
    elif sub:
        plan = _get_plan(db, sub.plan_id, role)
    if not plan:
        # Default plan is whichever has is_default=1
        Model = ImporterPlan if role == ROLE_IMPORTER else ExporterPlan
        plan = db.query(Model).filter(Model.is_default == 1).first()
    return success({
        "subscription": _serialize_subscription(sub, plan.title if plan else None) if sub else None,
        "current_plan": {
            "id": plan.id if plan else None,
            "title": plan.title if plan else None,
            "monthly_subscription_fee": f"{float(plan.monthly_subscription_fee):.2f}" if plan else None,
            "currency": plan.currency if plan else None,
            "is_default": plan.is_default if plan else None,
        } if plan else None,
        "plan_renewal_date": user.plan_renewal_date.isoformat() if user.plan_renewal_date else None,
        "plan_auto_renew": user.plan_auto_renew,
    })


# ───────────────────────── Importer routes ─────────────────────────

importer_router = APIRouter(prefix="/imp/subscription", tags=["importer-subscription"])


@importer_router.get("")
def imp_current(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    return _current(db, user, ROLE_IMPORTER)


@importer_router.post("/upgrade")
@limiter.limit("10/minute")
def imp_upgrade(
    request: Request,
    plan_id: str = Form(...),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    return _upgrade(db, user, ROLE_IMPORTER, plan_id)


@importer_router.post("/verify")
async def imp_verify(
    tx_ref: str = Form(...),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    return await _verify(db, user, tx_ref)


@importer_router.post("/cancel")
def imp_cancel(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    return _cancel(db, user)


# ───────────────────────── Exporter routes ─────────────────────────

exporter_router = APIRouter(prefix="/exp/subscription", tags=["exporter-subscription"])


@exporter_router.get("")
def exp_current(user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    return _current(db, user, ROLE_EXPORTER)


@exporter_router.post("/upgrade")
@limiter.limit("10/minute")
def exp_upgrade(
    request: Request,
    plan_id: str = Form(...),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    return _upgrade(db, user, ROLE_EXPORTER, plan_id)


@exporter_router.post("/verify")
async def exp_verify(
    tx_ref: str = Form(...),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    return await _verify(db, user, tx_ref)


@exporter_router.post("/cancel")
def exp_cancel(user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    return _cancel(db, user)
