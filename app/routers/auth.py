"""Auth - register/login/verify-email per role."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..config import get_settings
from ..constants import ROLE_ADMIN, ROLE_EXPORTER, ROLE_IMPORTER
from ..database import get_db
from ..envelope import fail, success
from ..models import BusinessProfile, EmailVerificationToken, User
from ..rate_limit import limiter
from ..schemas.auth import BusinessDTO, LoginPayload, UserDTO
from ..security import create_access_token, hash_password, secure_token, verify_password
from ..services.email import (
    send_template,
    t_account_under_review,
    t_welcome_verify,
    verification_email,
)

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def _login_payload(user: User, token: str) -> dict:
    biz = user.business
    return {
        "id": user.id,
        "firstname": user.firstname,
        "middlename": user.middlename,
        "lastname": user.lastname,
        "phone": user.phone,
        "address": user.address,
        "city": user.city,
        "state": user.state,
        "country": user.country,
        "dob": user.dob,
        "profile_name": user.profile_name,
        "fav_product": [],
        "product_delivered": user.product_delivered or 0,
        "review_count": user.review_count or 0,
        "status": user.status or 1,
        "business": {
            "business_name": biz.business_name if biz else None,
            "business_email": biz.business_email if biz else None,
            "business_address": biz.business_address if biz else None,
            "business_reg_number": biz.business_reg_number if biz else None,
            "business_type": biz.business_type if biz else None,
            "business_country": biz.business_country if biz else None,
            "annual_turnover": biz.annual_turnover if biz else None,
            "duration_in_business": biz.duration_in_business if biz else None,
            "documents": biz.documents if biz else None,
            "tin": biz.tin if biz else None,
            "valid_identification": biz.valid_identification if biz else None,
        },
        "token": token,
    }


# ───────────────────────── Login (per role) ─────────────────────────

def _do_login(db: Session, email: str, password: str, expected_role: str):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        raise fail("Email or password is incorrect", code=401)
    if user.role != expected_role:
        raise fail(f"This account is not registered as {expected_role}", code=403)
    if not user.is_active:
        raise fail("Account is suspended - contact support", code=403)
    # 2FA challenge: don't issue a token yet; frontend will call /auth/2fa/login
    if user.totp_enabled:
        return success(
            {"requires_2fa": True, "email": user.email},
            message="Two-factor code required",
        )
    token = create_access_token(subject=user.id, role=user.role)
    return success(_login_payload(user, token), message="Login successful")


@router.post("/imp/login")
@limiter.limit("10/minute")
def login_importer(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    return _do_login(db, payload.email, payload.password, ROLE_IMPORTER)


@router.post("/exp/login")
@limiter.limit("10/minute")
def login_exporter(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    return _do_login(db, payload.email, payload.password, ROLE_EXPORTER)


@router.post("/adm/login")
@limiter.limit("10/minute")
def login_admin(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    return _do_login(db, payload.email, payload.password, ROLE_ADMIN)


# ───────────────────────── Register (per role) ─────────────────────────

def _check_unique_email(db: Session, email: str) -> None:
    if db.query(User).filter(User.email == email).first():
        raise fail("An account with this email already exists", code=409)


def _send_verification_email(db: Session, user: User) -> str:
    code = secure_token(24)
    db.add(EmailVerificationToken(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    ))
    db.commit()
    s = get_settings()
    link = f"{s.site_url}/auth/verify-email?code={code}&role={user.role}"
    subject, html = t_welcome_verify(user.firstname or "there", link, code)
    send_template(
        db,
        template="welcome_verify",
        to=user.email,
        subject=subject,
        html=html,
        user_id=user.id,
        dedupe_key=f"welcome_verify:{code}",
    )
    return code


@router.put("/imp/register")
def register_importer(
    db: Session = Depends(get_db),
    type: str = Form(default="individual"),
    firstname: str = Form(...),
    lastname: str = Form(...),
    phone: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(..., min_length=8),
    profile_name: str = Form(...),
    # Address + DOB used to be required at signup. They moved to the
    # post-signup profile flow ("slim signup" v3.7), so the slim form
    # only sends the 6 essentials. We keep them as optional inputs so
    # legacy clients that still send them continue to work.
    address: Optional[str] = Form(default=None),
    dob: Optional[str] = Form(default=None),
    business_name: Optional[str] = Form(default=None),
    business_reg_num: Optional[str] = Form(default=None),
    business_email: Optional[str] = Form(default=None),
    business_address: Optional[str] = Form(default=None),
    valid_ID: Optional[str] = Form(default=None),
):
    _check_unique_email(db, email)
    user = User(
        role=ROLE_IMPORTER,
        kind="business" if type == "business" else "individual",
        email=email,
        password_hash=hash_password(password),
        firstname=firstname,
        lastname=lastname,
        phone=phone,
        address=address,
        dob=dob,
        profile_name=profile_name,
        is_active=True,
        email_verified=False,
        valid_identification=valid_ID,
    )
    db.add(user)
    db.flush()

    if type == "business":
        if not business_name:
            raise fail("Business name is required for business accounts")
        db.add(BusinessProfile(
            user_id=user.id,
            business_name=business_name,
            business_email=business_email,
            business_address=business_address,
            business_reg_number=business_reg_num,
        ))
    db.commit()
    _send_verification_email(db, user)
    return success({"id": user.id}, message="Account created - check your email to verify.")


@router.put("/exp/register")
def register_exporter(
    db: Session = Depends(get_db),
    # Six essentials collected on the slim v3.7 signup form ─────────────────
    firstname: str = Form(...),
    lastname: str = Form(...),
    phone: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(..., min_length=8),
    profile_name: str = Form(...),
    # ── Everything below is deferred to the post-signup profile / KYC flow.
    # All optional so the slim signup form (which only sends the six above)
    # works. KYC review still gates `is_active`, so an exporter that hasn't
    # filled these in can't transact regardless.
    business_name: Optional[str] = Form(default=None),
    business_reg_num: Optional[str] = Form(default=None),
    business_email: Optional[str] = Form(default=None),
    business_address: Optional[str] = Form(default=None),
    duration_in_business: Optional[str] = Form(default=None),
    annual_turnover: Optional[str] = Form(default=None),
    valid_ID: Optional[str] = Form(default=None),
    business_type: Optional[str] = Form(default=None),
    business_country: Optional[str] = Form(default=None),
    market_locations: Optional[str] = Form(default=None),
    bank_id: Optional[str] = Form(default=None),
    account_name: Optional[str] = Form(default=None),
    account_number: Optional[str] = Form(default=None),
    TIN: Optional[str] = Form(default=None),
    dob: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
    address: Optional[str] = Form(default=None),
    type: str = Form(default="business"),  # ignored - exporters are always business
):
    _check_unique_email(db, email)
    try:
        duration_int = int(duration_in_business) if duration_in_business else None
    except (TypeError, ValueError):
        duration_int = None

    user = User(
        role=ROLE_EXPORTER,
        kind="business",
        email=email,
        password_hash=hash_password(password),
        firstname=firstname,
        lastname=lastname,
        phone=phone,
        address=address,
        country=country,
        dob=dob,
        profile_name=profile_name,
        is_active=False,  # exporters need admin verification
        email_verified=False,
        valid_identification=valid_ID,
    )
    db.add(user)
    db.flush()

    # Only create the BusinessProfile if the exporter actually filled in
    # business details at signup. Otherwise we leave it null; the post-
    # signup profile-update endpoint will lazy-create it the moment they
    # save business details. business_name is the NOT NULL field on BP,
    # so it's the gate.
    if business_name:
        db.add(BusinessProfile(
            user_id=user.id,
            business_name=business_name,
            business_email=business_email,
            business_address=business_address,
            business_reg_number=business_reg_num,
            business_type=business_type,
            business_country=business_country,
            annual_turnover=annual_turnover,
            duration_in_business=duration_int,
            tin=TIN,
            bank_id=bank_id,
            account_name=account_name,
            account_number=account_number,
            valid_identification=valid_ID,
        ))
    user.kyc_status = "pending"
    db.commit()
    _send_verification_email(db, user)
    # KYC under-review notice
    subject, html = t_account_under_review(user.firstname or "there")
    send_template(
        db,
        template="account_under_review",
        to=user.email,
        subject=subject,
        html=html,
        user_id=user.id,
        dedupe_key=f"under_review:{user.id}",
    )
    return success({"id": user.id}, message="Application submitted - we'll review and email you when activated.")


@router.post("/adm/register")
def register_admin(
    db: Session = Depends(get_db),
    firstname: str = Form(...),
    lastname: str = Form(...),
    phone: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(..., min_length=8),
    role: str = Form(default="admin"),
):
    _check_unique_email(db, email)
    user = User(
        role=ROLE_ADMIN,
        kind="business",
        email=email,
        password_hash=hash_password(password),
        firstname=firstname,
        lastname=lastname,
        phone=phone,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    return success({"id": user.id}, message="Admin user created")


# ───────────────────────── Email verification ─────────────────────────

def _verify_account(db: Session, code: str, expected_role: str):
    token = db.query(EmailVerificationToken).filter(EmailVerificationToken.code == code).first()
    if not token or token.used or token.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise fail("Verification link is invalid or expired", code=400)
    user = db.get(User, token.user_id)
    if not user or user.role != expected_role:
        raise fail("Account not found", code=404)
    user.email_verified = True
    if expected_role == ROLE_IMPORTER:
        user.is_active = True  # importers self-activate on email verify
    token.used = True
    db.commit()
    return success({"verified": True}, message="Email verified")


@router.post("/imp/account_verification")
def verify_importer(code: str = Form(...), db: Session = Depends(get_db)):
    return _verify_account(db, code, ROLE_IMPORTER)


@router.post("/exp/account_verification")
def verify_exporter(code: str = Form(...), db: Session = Depends(get_db)):
    return _verify_account(db, code, ROLE_EXPORTER)


def _request_verify(db: Session, email: str, expected_role: str):
    user = db.query(User).filter(User.email == email, User.role == expected_role).first()
    if user and not user.email_verified:
        _send_verification_email(db, user)
    return success({"sent": True}, message="If the account exists, a verification email has been sent.")


@router.get("/imp/account_verification")
def request_verify_importer(email: str = Query(...), u: str = Query(default="importer"), db: Session = Depends(get_db)):
    return _request_verify(db, email, ROLE_IMPORTER)


@router.get("/exp/account_verification")
def request_verify_exporter(email: str = Query(...), u: str = Query(default="exporter"), db: Session = Depends(get_db)):
    return _request_verify(db, email, ROLE_EXPORTER)
