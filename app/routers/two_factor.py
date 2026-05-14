"""Two-factor authentication (TOTP, RFC 6238).

Flow:
  1. Authed user calls `POST /auth/2fa/enroll` -> server generates TOTP secret +
     returns provisioning URI + base32 secret. User scans/pastes into their
     authenticator app.
  2. User calls `POST /auth/2fa/confirm` with a 6-digit code; on success we set
     `totp_enabled=True`.
  3. On subsequent logins, server returns `requires_2fa: True` instead of a
     token. Frontend prompts for code, calls `POST /auth/2fa/login` with email +
     password + code -> token issued.

We also expose `POST /auth/2fa/disable` for users to turn 2FA off.
"""
from __future__ import annotations

from typing import Optional

import pyotp
from fastapi import APIRouter, Depends, Form
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..envelope import fail, success
from ..models import User
from ..security import create_access_token, verify_password
from ..services.email import send_template, t_2fa_enabled, t_account_updated

router = APIRouter(prefix="/auth/2fa", tags=["2fa"])
settings = get_settings()

ISSUER = "Jaratrade"


def _provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


@router.post("/enroll")
def enroll_2fa(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate a TOTP secret. The frontend renders the provisioning URI as a QR code."""
    if user.totp_enabled:
        raise fail("2FA is already enabled. Disable it first to re-enroll.", code=409)
    secret = pyotp.random_base32()
    user.totp_secret = secret
    db.commit()
    return success({
        "secret": secret,
        "issuer": ISSUER,
        "label": user.email,
        "uri": _provisioning_uri(secret, user.email),
    })


@router.post("/confirm")
def confirm_2fa(
    code: str = Form(..., min_length=6, max_length=6),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.totp_enabled:
        raise fail("2FA is already enabled", code=409)
    if not user.totp_secret:
        raise fail("Start enrollment first", code=400)
    if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
        raise fail("Invalid code", code=400)
    user.totp_enabled = True
    db.commit()
    subject, html = t_2fa_enabled(user.firstname or "there")
    send_template(db, template="2fa_enabled", to=user.email, subject=subject, html=html, user_id=user.id)
    return success({"enabled": True})


@router.post("/disable")
def disable_2fa(
    password: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Require the user's password to disable - prevents session-takeover bypass."""
    if not verify_password(password, user.password_hash):
        raise fail("Password is incorrect", code=401)
    user.totp_enabled = False
    user.totp_secret = None
    db.commit()
    subject, html = t_account_updated(user.firstname or "there", "two-factor authentication setting")
    send_template(db, template="account_updated_2fa", to=user.email, subject=subject, html=html, user_id=user.id)
    return success({"disabled": True})


# ───────────────────────── Login challenge ─────────────────────────

class TwoFactorLoginIn(BaseModel):
    email: EmailStr
    password: str
    code: str


@router.post("/login")
def login_with_2fa(payload: TwoFactorLoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise fail("Email or password is incorrect", code=401)
    if not user.totp_enabled or not user.totp_secret:
        raise fail("2FA is not enabled on this account - log in normally", code=400)
    if not pyotp.TOTP(user.totp_secret).verify(payload.code, valid_window=1):
        raise fail("Invalid 2FA code", code=401)
    if not user.is_active:
        raise fail("Account is suspended", code=403)
    token = create_access_token(subject=user.id, role=user.role)
    return success({"token": token, "id": user.id, "role": user.role}, message="Login successful")
