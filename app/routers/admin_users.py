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
from ..models import User, BusinessProfile
from ..services.email import send_template, t_account_activated, t_account_rejected

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
    """List pending exporter KYC applications."""
    q = db.query(User).filter(User.role == ROLE_EXPORTER, User.kyc_status == "pending")
    total = q.count()
    rows = q.order_by(User.time_created.asc()).offset(p * len_).limit(len_).all()
    return success({
        "rows": [_serialize_user(u) for u in rows],
        "total_length": total,
        "page": p,
        "len": len_,
    })


@router.post("/kyc/{user_id}/approve")
def kyc_approve(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise fail("User not found", code=404)
    if u.role != ROLE_EXPORTER:
        raise fail("Only exporter accounts go through KYC", code=400)
    u.kyc_status = "approved"
    u.kyc_reviewed_at = datetime.now(timezone.utc)
    u.kyc_rejection_reason = None
    u.is_active = True
    u.email_verified = True
    db.commit()
    db.refresh(u)
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
