"""Email verification flow: 6-digit OTP scoped by (email, code).

Switched from the old 24-byte URL-safe token because:
  (a) long opaque tokens got mangled by some email clients between the
      displayed code and the link href (user reported: link says expired
      but pasting the same code works), and
  (b) typing/pasting a 32-char string on mobile is hostile UX.

Both verify endpoints now require email + code, and are idempotent so a
re-click of the email link (or a pre-fetch by an inbox security scanner)
on an already-verified user returns success rather than "expired".
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import EmailVerificationToken, User
from app.routers.auth import _new_otp


def _register_importer(client: TestClient, email: str) -> None:
    """Helper: sign up an importer (slim form) and assert success."""
    r = client.put("/imp/register", data={
        "type": "individual",
        "firstname": "Test",
        "lastname": "User",
        "phone": "+447700900000",
        "email": email,
        "password": "test-password-123",
        "profile_name": "test-user",
    })
    assert r.status_code == 200, r.text


def _latest_unused_code(db: Session, email: str) -> str:
    user = db.query(User).filter(User.email == email).first()
    assert user is not None
    token = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.user_id == user.id, EmailVerificationToken.used.is_(False))
        .order_by(EmailVerificationToken.time_created.desc())
        .first()
    )
    assert token is not None, f"No unused verification token for {email}"
    return token.code


# ── Generator + format ─────────────────────────────────────────────────────

def test_new_otp_is_six_digits():
    """Every code from _new_otp is exactly 6 numeric chars, zero-padded."""
    for _ in range(50):
        code = _new_otp()
        assert re.fullmatch(r"\d{6}", code), code


# ── Happy path ─────────────────────────────────────────────────────────────

def test_verify_email_with_otp_succeeds(client: TestClient):
    email = "verify-otp-happy@example.com"
    _register_importer(client, email)
    with SessionLocal() as db:
        code = _latest_unused_code(db, email)

    r = client.post("/imp/account_verification", data={"email": email, "code": code})
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["verified"] is True


# ── Idempotency (the user's reported bug) ──────────────────────────────────

def test_verify_email_is_idempotent_after_first_success(client: TestClient):
    """If a user (or an inbox security scanner) re-hits the verify endpoint
    after the account is already verified, return success - not 'expired'.
    This is the regression for the original bug report: clicking the email
    link said the link was expired but pasting the code worked. With this
    idempotency the second hit also returns success."""
    email = "verify-otp-idempotent@example.com"
    _register_importer(client, email)
    with SessionLocal() as db:
        code = _latest_unused_code(db, email)

    # First verify - succeeds
    r1 = client.post("/imp/account_verification", data={"email": email, "code": code})
    assert r1.status_code == 200, r1.text
    assert r1.json()["payload"]["verified"] is True

    # Second verify with the SAME code (now used) - should still succeed
    # because the user is already verified.
    r2 = client.post("/imp/account_verification", data={"email": email, "code": code})
    assert r2.status_code == 200, r2.text
    p2 = r2.json()["payload"]
    assert p2["verified"] is True
    assert p2.get("already") is True


# ── Failure paths ──────────────────────────────────────────────────────────

def test_verify_email_wrong_code_fails(client: TestClient):
    email = "verify-otp-wrongcode@example.com"
    _register_importer(client, email)

    r = client.post("/imp/account_verification", data={"email": email, "code": "000000"})
    assert r.status_code == 400, r.text
    assert "invalid" in r.text.lower() or "expired" in r.text.lower()


def test_verify_email_unknown_email_fails_same_shape(client: TestClient):
    """Unknown email returns the same error as wrong code - we don't leak
    which emails are registered."""
    r = client.post("/imp/account_verification", data={
        "email": "no-such-user@example.com",
        "code": "123456",
    })
    assert r.status_code == 400, r.text
    assert "invalid" in r.text.lower() or "expired" in r.text.lower()


def test_verify_email_collision_across_users_is_scoped(client: TestClient):
    """Two users can have the same 6-digit OTP - the lookup must scope by
    user, otherwise a colliding code would verify the WRONG account. This
    test forces a collision by manually inserting matching codes."""
    email_a = "verify-otp-collide-a@example.com"
    email_b = "verify-otp-collide-b@example.com"
    _register_importer(client, email_a)
    _register_importer(client, email_b)

    shared = "424242"
    with SessionLocal() as db:
        # Replace each user's latest unused token's code with `shared` so the
        # collision exists. Real-world this would just be a 1-in-1M dice roll.
        for email in (email_a, email_b):
            user = db.query(User).filter(User.email == email).first()
            token = (
                db.query(EmailVerificationToken)
                .filter(EmailVerificationToken.user_id == user.id,
                        EmailVerificationToken.used.is_(False))
                .first()
            )
            token.code = shared
        db.commit()

    # Submitting (email_a, shared) verifies a, NOT b.
    r = client.post("/imp/account_verification", data={"email": email_a, "code": shared})
    assert r.status_code == 200
    with SessionLocal() as db:
        a = db.query(User).filter(User.email == email_a).first()
        b = db.query(User).filter(User.email == email_b).first()
        assert a.email_verified is True
        assert b.email_verified is False, "User B got verified by A's code; lookup isn't user-scoped"


# ── Token rotation (a fresh email invalidates older codes) ─────────────────

def test_resend_invalidates_prior_codes(client: TestClient):
    """When a user requests a fresh verification email, any prior unused
    code should be marked used so an old email lying around can't quietly
    verify the account behind the user's back."""
    email = "verify-otp-rotate@example.com"
    _register_importer(client, email)
    with SessionLocal() as db:
        old_code = _latest_unused_code(db, email)

    # Request a fresh email - this generates a new code and marks the old
    # one used. (Resend endpoint is a GET with email + role.)
    r = client.get("/imp/account_verification", params={"email": email, "u": "importer"})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        new_code = _latest_unused_code(db, email)
        assert new_code != old_code, "Resend should generate a fresh code"

    # The OLD code should now fail to verify.
    r = client.post("/imp/account_verification", data={"email": email, "code": old_code})
    assert r.status_code == 400, r.text
