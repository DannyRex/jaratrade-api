"""Exporter KYC submission lifecycle tests.

Covers the v3.8 "submit for review" flow:
  - a freshly-signed-up exporter is is_active=True (can recover into the
    app via email verification, not hard-locked with "account suspended")
  - POST /exp/submit-for-review rejects an incomplete profile with the
    list of missing fields
  - submitting a complete profile stamps kyc_submitted_at
  - /adm/kyc/queue only surfaces submitted exporters
  - /adm/kyc/{id}/approve refuses an exporter who hasn't submitted
"""
from app.database import SessionLocal
from app.models import Bank, User


def _register_exporter(client, email: str) -> str:
    """Slim-signup an exporter, flip email_verified so login works, return token."""
    r = client.put("/exp/register", data={
        "firstname": "Kyc", "lastname": "Tester", "phone": f"+23481{abs(hash(email)) % 10**8:08d}",
        "email": email, "password": "password123", "profile_name": email.split("@")[0],
    })
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        u.email_verified = True  # skip the email-link step for the test
        db.commit()
    r = client.post("/exp/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]


def _complete_profile(client, token: str) -> None:
    """Fill in every field the submit-for-review completeness check needs."""
    with SessionLocal() as db:
        bank = db.query(Bank).filter(Bank.flutter_code.isnot(None)).first()
        assert bank is not None, "seed should include a bank with a flutter_code"
        bank_id = bank.id
    r = client.post(
        "/exp/profile",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "business_name": "Kyc Test Foods Ltd",
            "business_email": "biz@kyctest.com",
            "business_address": "12 Market Street, Lagos",
            "business_reg_num": "RC998877",
            "business_type": "food_beverage",
            "tin": "TIN-998877",
            "bank_id": bank_id,
            "account_name": "Kyc Test Foods Ltd",
            "account_number": "0123456789",
        },
    )
    assert r.status_code == 200, r.text
    # The means-of-ID document is a required upload, not a text field.
    r = client.post(
        "/exp/kyc-document",
        headers={"Authorization": f"Bearer {token}"},
        data={"doc_type": "id"},
        files={"file": ("id.png", b"fake-id-scan-bytes", "image/png")},
    )
    assert r.status_code == 200, r.text


# ───────────────────────── is_active fix ─────────────────────────

def test_new_exporter_is_active_and_not_hard_locked(client):
    """Regression: register_exporter used to set is_active=False, and
    _do_login rejected inactive accounts with 'Account is suspended' - so a
    new exporter could never log in to finish onboarding. Now they're active;
    login returns the recoverable requires_verification state, not 'suspended'.
    """
    r = client.put("/exp/register", data={
        "firstname": "Fresh", "lastname": "Exporter", "phone": "+2348100000123",
        "email": "fresh-exporter@example.com", "password": "password123",
        "profile_name": "fresh-exp",
    })
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "fresh-exporter@example.com").first()
        assert u.is_active is True
        assert u.kyc_status == "pending"
        assert u.kyc_submitted_at is None

    # Login should NOT say "suspended" - it should hand back the
    # requires_verification challenge (recoverable).
    r = client.post("/exp/login", json={
        "email": "fresh-exporter@example.com", "password": "password123",
    })
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload.get("requires_verification") is True


# ───────────────────────── submit-for-review ─────────────────────────

def test_submit_for_review_rejects_incomplete_profile(client):
    token = _register_exporter(client, "incomplete-exp@example.com")
    # No business profile filled in at all.
    r = client.post("/exp/submit-for-review", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.text
    body = r.text.lower()
    # The error names the missing items.
    assert "business name" in body
    assert "bank account" in body


def test_submit_for_review_succeeds_on_complete_profile(client):
    token = _register_exporter(client, "complete-exp@example.com")
    _complete_profile(client, token)

    r = client.post("/exp/submit-for-review", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["kyc_submitted_at"]

    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "complete-exp@example.com").first()
        assert u.kyc_submitted_at is not None
        assert u.kyc_status == "pending"


def test_profile_get_reports_kyc_missing_fields(client):
    """The /exp/profile payload drives the frontend Submit button - it must
    report kyc_missing_fields (non-empty before completion, empty after)."""
    token = _register_exporter(client, "missing-fields-exp@example.com")
    r = client.get("/exp/profile", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert len(r.json()["payload"]["kyc_missing_fields"]) > 0

    _complete_profile(client, token)
    r = client.get("/exp/profile", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["payload"]["kyc_missing_fields"] == []


# ───────────────────────── admin queue + approve guard ─────────────────────────

def test_kyc_queue_excludes_unsubmitted_exporters(client, admin_token):
    """An exporter who signed up but hasn't submitted must NOT appear in the
    admin KYC queue - there's nothing to review."""
    _register_exporter(client, "unsubmitted-exp@example.com")  # registered, not submitted

    r = client.get("/adm/kyc/queue", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    emails = [row["email"] for row in r.json()["payload"]["rows"]]
    assert "unsubmitted-exp@example.com" not in emails


def test_kyc_queue_includes_submitted_exporter(client, admin_token):
    token = _register_exporter(client, "queued-exp@example.com")
    _complete_profile(client, token)
    client.post("/exp/submit-for-review", headers={"Authorization": f"Bearer {token}"})

    r = client.get("/adm/kyc/queue", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    queued = next((u for u in rows if u["email"] == "queued-exp@example.com"), None)
    assert queued is not None
    assert queued["kyc_submitted_at"] is not None


def test_kyc_approve_refuses_unsubmitted_exporter(client, admin_token):
    """Admin cannot approve an exporter who never submitted for review."""
    _register_exporter(client, "noapprove-exp@example.com")
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == "noapprove-exp@example.com").first().id

    r = client.post(
        f"/adm/kyc/{uid}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400, r.text
    assert "submitted" in r.text.lower()


def test_kyc_approve_works_after_submission(client, admin_token):
    """Full happy path: complete profile -> submit -> admin approves."""
    token = _register_exporter(client, "happypath-exp@example.com")
    _complete_profile(client, token)
    client.post("/exp/submit-for-review", headers={"Authorization": f"Bearer {token}"})

    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == "happypath-exp@example.com").first().id

    r = client.post(
        f"/adm/kyc/{uid}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["kyc_status"] == "approved"


# ───────────────────────── KYC document upload ─────────────────────────

def test_kyc_document_upload_stores_url(client):
    token = _register_exporter(client, "docupload-exp@example.com")
    r = client.post(
        "/exp/kyc-document",
        headers={"Authorization": f"Bearer {token}"},
        data={"doc_type": "id"},
        files={"file": ("passport.png", b"fake-scan", "image/png")},
    )
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["doc_type"] == "id"
    assert payload["url"]
    assert payload["documents"].get("id") == payload["url"]

    # It shows up on the profile + clears the missing-field entry.
    r = client.get("/exp/profile", headers={"Authorization": f"Bearer {token}"})
    p = r.json()["payload"]
    assert p["documents"].get("id")
    assert "Means of ID document" not in p["kyc_missing_fields"]


def test_kyc_document_rejects_bad_doc_type(client):
    token = _register_exporter(client, "baddoctype-exp@example.com")
    r = client.post(
        "/exp/kyc-document",
        headers={"Authorization": f"Bearer {token}"},
        data={"doc_type": "selfie"},
        files={"file": ("x.png", b"bytes", "image/png")},
    )
    assert r.status_code == 400
    assert "doc_type" in r.text.lower()


def test_kyc_document_rejects_unsupported_extension(client):
    token = _register_exporter(client, "badext-exp@example.com")
    r = client.post(
        "/exp/kyc-document",
        headers={"Authorization": f"Bearer {token}"},
        data={"doc_type": "id"},
        files={"file": ("malware.exe", b"bytes", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "file type" in r.text.lower()


def test_submit_for_review_blocked_without_id_document(client):
    """A profile with every text field filled but no ID document uploaded
    must still fail the completeness check."""
    from app.database import SessionLocal
    from app.models import Bank

    token = _register_exporter(client, "noiddoc-exp@example.com")
    with SessionLocal() as db:
        bank_id = db.query(Bank).filter(Bank.flutter_code.isnot(None)).first().id
    client.post(
        "/exp/profile",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "business_name": "No ID Doc Ltd", "business_email": "b@noid.com",
            "business_address": "1 St", "business_reg_num": "RC1", "tin": "TIN1",
            "bank_id": bank_id, "account_name": "No ID Doc Ltd",
            "account_number": "0123456789",
        },
    )
    r = client.post("/exp/submit-for-review", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "means of id document" in r.text.lower()
