"""Admin users + KYC verification queue.

Closes the documented backend gap (`GET /adm/users`) and adds the KYC review
flow (list pending exporters, approve, reject - with notification emails).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from ..config import get_settings
from ..constants import ROLE_EXPORTER, ROLE_IMPORTER
from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import Bank, User, BusinessProfile
from ..services.email import send_template, t_account_activated, t_account_rejected
from ..services.flutterwave import FlutterwaveError, create_subaccount

router = APIRouter(prefix="/adm", tags=["admin"])
settings = get_settings()


def _serialize_user(u: User) -> dict:
    biz = u.business
    return {
        "id": u.id,
        "role": u.role,
        "kind": u.kind,
        "email": u.email,
        "firstname": u.firstname,
        "lastname": u.lastname,
        "fullname": u.fullname,
        "phone": u.phone,
        "country": u.country,
        "profile_name": u.profile_name,
        "is_active": u.is_active,
        "email_verified": u.email_verified,
        "kyc_status": u.kyc_status,
        "kyc_submitted_at": u.kyc_submitted_at.isoformat() if u.kyc_submitted_at else None,
        "kyc_reviewed_at": u.kyc_reviewed_at.isoformat() if u.kyc_reviewed_at else None,
        "kyc_rejection_reason": u.kyc_rejection_reason,
        "totp_enabled": u.totp_enabled,
        "plan_id": u.plan_id,
        "monthly_spent": f"{float(u.monthly_spent or 0):.2f}",
        "review_count": u.review_count,
        "product_delivered": u.product_delivered,
        "business_name": biz.business_name if biz else None,
        "business_country": biz.business_country if biz else None,
        "business_reg_number": biz.business_reg_number if biz else None,
        # FLW subaccount provisioning state - admins use this to spot
        # exporters who passed KYC but couldn't be provisioned (bad bank
        # details, etc) so they can be retried manually.
        "flw_subaccount_id": u.flw_subaccount_id,
        "time_created": u.time_created.isoformat(),
    }


# ───────────────────────── User search ─────────────────────────

@router.get("/users")
def list_users(
    role: Optional[str] = Query(default=None, pattern="^(importer|exporter|admin)$"),
    is_active: Optional[bool] = Query(default=None),
    kyc_status: Optional[str] = Query(default=None, pattern="^(pending|approved|rejected)$"),
    q: Optional[str] = Query(default=None, min_length=1),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=50, ge=1, le=200, alias="len"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Search/filter users. Closes the legacy `GET /adm/users` gap."""
    query = db.query(User)
    if role:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    if kyc_status:
        query = query.filter(User.kyc_status == kyc_status)
    if q:
        like = f"%{q}%"
        query = (
            query.outerjoin(BusinessProfile, BusinessProfile.user_id == User.id)
            .filter(or_(
                User.email.ilike(like),
                User.firstname.ilike(like),
                User.lastname.ilike(like),
                User.profile_name.ilike(like),
                BusinessProfile.business_name.ilike(like),
            ))
        )
    total = query.distinct().count()
    rows = query.order_by(desc(User.time_created)).offset(p * len_).limit(len_).all()
    return success({
        "rows": [_serialize_user(u) for u in rows],
        "total_length": total,
        "page": p,
        "len": len_,
    })


@router.get("/users/{user_id}")
def get_user(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    return success(_serialize_user(u))


# ───────────────────────── KYC queue ─────────────────────────

@router.get("/kyc/queue")
def kyc_queue(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=50, ge=1, le=200, alias="len"),
):
    """List exporter KYC applications that are *ready for review*.

    "Ready" = pending status AND the exporter has pressed "Submit for
    review" (kyc_submitted_at is set). Exporters who've signed up but
    not yet completed + submitted their profile are deliberately
    excluded - there's nothing for an admin to review, and surfacing
    them invites approving an empty application.

    Oldest submission first so the queue is FIFO.
    """
    q = db.query(User).filter(
        User.role == ROLE_EXPORTER,
        User.kyc_status == "pending",
        User.kyc_submitted_at.isnot(None),
    )
    total = q.count()
    rows = q.order_by(User.kyc_submitted_at.asc()).offset(p * len_).limit(len_).all()
    return success({
        "rows": [_serialize_user(u) for u in rows],
        "total_length": total,
        "page": p,
        "len": len_,
    })


@router.post("/kyc/{user_id}/approve")
async def kyc_approve(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Approve an exporter's KYC + auto-provision their Flutterwave subaccount.

    The subaccount is the destination Flutterwave routes the seller's share
    of each order's split into. Approving without provisioning would leave
    the exporter unable to actually receive funds, so we attempt it inline.
    Failures don't block approval - admins can retry via POST
    /adm/users/{id}/reprovision-subaccount.
    """
    import json
    import traceback

    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    if u.role != ROLE_EXPORTER:
        raise fail("Only exporter accounts go through KYC", code=400)
    # Structurally prevent approving an exporter who hasn't completed +
    # submitted their business profile. Without this an admin could approve
    # an empty application straight from signup - and the FLW subaccount
    # provisioning below would fail anyway (no bank details on file).
    if u.kyc_submitted_at is None:
        raise fail(
            "This exporter hasn't submitted their profile for review yet. "
            "They need to complete their business details and bank account first.",
            code=400,
        )

    u.kyc_status = "approved"
    u.kyc_reviewed_at = datetime.now(timezone.utc)
    u.kyc_rejection_reason = None
    u.is_active = True
    u.email_verified = True
    db.commit()
    db.refresh(u)

    # Provision subaccount if we have enough banking info on file. Quietly
    # records the error if we don't - admin can retry separately.
    if not u.flw_subaccount_id and u.business and u.business.bank_id and u.business.account_number:
        bank = db.get(Bank, u.business.bank_id)
        if bank and (bank.flutter_code or bank.paystack_code):
            account_bank = bank.flutter_code or bank.paystack_code or ""
            try:
                resp = await create_subaccount(
                    account_bank=account_bank,
                    account_number=u.business.account_number,
                    business_name=u.business.business_name or u.fullname or u.email,
                    business_email=u.business.business_email or u.email,
                    business_mobile=u.phone or "0000000000",
                    country=u.country or "NG",
                )
                sub_id = resp.get("subaccount_id") or resp.get("id")
                if sub_id:
                    u.flw_subaccount_id = str(sub_id)
                    u.flw_subaccount_payload = json.dumps(resp)
                    db.commit()
                    db.refresh(u)
            except FlutterwaveError as e:
                # Persist FLW's complaint for admin diagnostics; don't block.
                u.flw_subaccount_payload = json.dumps({"error": str(e), "body": e.body})
                db.commit()
            except Exception:  # noqa: BLE001
                # Don't block approval on a provisioning failure; admin can retry.
                traceback.print_exc()

    subject, html = t_account_activated(u.firstname or "there", f"{settings.site_url}/auth/login/exporter")
    send_template(
        db,
        template="account_activated",
        to=u.email,
        subject=subject,
        html=html,
        user_id=u.id,
        dedupe_key=f"activated:{u.id}",
    )
    return success(_serialize_user(u))


@router.post("/users/{user_id}/reprovision-subaccount")
async def reprovision_subaccount(
    user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """Manually (re-)trigger Flutterwave subaccount provisioning for an
    approved exporter. Used when the auto-provision at approve time failed
    (e.g. bad bank details now fixed, FLW outage, etc).
    """
    import json
    import traceback

    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    if u.role != ROLE_EXPORTER:
        raise fail("Only exporter accounts have subaccounts", code=400)
    if u.kyc_status != "approved":
        raise fail("Approve KYC first", code=400)
    if not (u.business and u.business.bank_id and u.business.account_number):
        raise fail("Seller's bank account isn't on file yet", code=400)
    bank = db.get(Bank, u.business.bank_id)
    if not bank or not (bank.flutter_code or bank.paystack_code):
        raise fail("Selected bank has no Flutterwave code mapped", code=400)
    account_bank = bank.flutter_code or bank.paystack_code or ""

    try:
        resp = await create_subaccount(
            account_bank=account_bank,
            account_number=u.business.account_number,
            business_name=u.business.business_name or u.fullname or u.email,
            business_email=u.business.business_email or u.email,
            business_mobile=u.phone or "0000000000",
            country=u.country or "NG",
        )
    except FlutterwaveError as e:
        # Surface FLW's own error message rather than a Python repr.
        raise fail(f"Flutterwave rejected the request: {e.status_code} - {e.body}", code=502)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise fail(f"Flutterwave call failed: {e!r}", code=502)

    sub_id = resp.get("subaccount_id") or resp.get("id")
    if not sub_id:
        raise fail("Flutterwave returned no subaccount_id", code=502)
    u.flw_subaccount_id = str(sub_id)
    u.flw_subaccount_payload = json.dumps(resp)
    db.commit()
    db.refresh(u)
    return success(_serialize_user(u))


@router.post("/users/{user_id}/resend-approval-email")
def resend_approval_email(
    user_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Manually re-send the "account approved" email.

    Useful when the original send failed silently (SMTP outage, blocked port,
    domain not verified, etc) and the user is stuck without their welcome
    email. Bypasses `send_template`'s dedupe by passing dedupe_key=None.
    """
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    if u.role != ROLE_EXPORTER:
        raise fail("Approval emails only apply to exporter accounts", code=400)
    if u.kyc_status != "approved":
        raise fail("This exporter isn't approved yet", code=400)

    subject, html = t_account_activated(
        u.firstname or "there",
        f"{settings.site_url}/auth/login/exporter",
    )
    send_template(
        db,
        template="account_activated_resend",
        to=u.email,
        subject=subject,
        html=html,
        user_id=u.id,
        # Deliberately no dedupe_key - admin asked for a re-send.
    )
    return success({"sent": True}, message=f"Approval email re-sent to {u.email}")


@router.post("/kyc/{user_id}/reject")
def kyc_reject(
    user_id: str,
    reason: str = Form(..., min_length=3),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    if u.role != ROLE_EXPORTER:
        raise fail("Only exporter accounts go through KYC", code=400)
    u.kyc_status = "rejected"
    u.kyc_reviewed_at = datetime.now(timezone.utc)
    u.kyc_rejection_reason = reason
    u.is_active = False
    db.commit()
    db.refresh(u)
    subject, html = t_account_rejected(u.firstname or "there", reason)
    send_template(
        db,
        template="account_rejected",
        to=u.email,
        subject=subject,
        html=html,
        user_id=u.id,
        dedupe_key=f"rejected:{u.id}:{u.kyc_reviewed_at.isoformat()}",
    )
    return success(_serialize_user(u))


@router.post("/users/{user_id}/suspend")
def suspend_user(
    user_id: str,
    reason: Optional[str] = Form(default=None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    u.is_active = False
    db.commit()
    return success({"suspended": True, "reason": reason})


@router.post("/users/{user_id}/reactivate")
def reactivate_user(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    u.is_active = True
    db.commit()
    return success({"reactivated": True})
