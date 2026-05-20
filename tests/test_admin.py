"""Admin endpoint tests: user search + KYC review."""


def test_admin_user_search(client, admin_token):
    r = client.get("/adm/users", params={"role": "exporter"},
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["total_length"] >= 1
    assert all(u["role"] == "exporter" for u in p["rows"])


def test_admin_user_search_filters_by_query(client, admin_token):
    r = client.get("/adm/users", params={"q": "Adaeze"},
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["total_length"] >= 1


def test_kyc_queue_starts_empty_after_seed(client, admin_token):
    """The seeded demo exporter is pre-approved, so the queue should be empty."""
    r = client.get("/adm/kyc/queue", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["payload"]["total_length"] == 0


def test_kyc_approve_reject_flow(client, admin_token):
    # 1. Register a new exporter (auto-status: pending)
    r = client.put("/exp/register", data={
        "firstname": "Newtest", "lastname": "Exporter", "phone": "+2348100009998",
        "email": "newtest@example.com", "password": "password123", "profile_name": "newtest",
        "business_name": "Newtest Co", "business_reg_num": "RC9999",
        "business_email": "biz@newtest.com", "business_address": "Stall 9",
        "duration_in_business": "2", "annual_turnover": "100k_1m",
        "valid_ID": "passport", "business_type": "food_beverage",
        "TIN": "TIN999", "dob": "1990-01-01", "country": "Nigeria", "address": "Lagos",
    })
    assert r.status_code == 200, r.text

    # 1b. Simulate the exporter pressing "Submit for review" - the admin KYC
    # queue + approve both now require kyc_submitted_at to be set. (The
    # submission flow itself is covered by tests/test_kyc_submission.py.)
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models import User
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "newtest@example.com").first()
        u.kyc_submitted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    # 2. Queue should now have one entry
    r = client.get("/adm/kyc/queue", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    pending = next((u for u in rows if u["email"] == "newtest@example.com"), None)
    assert pending is not None
    user_id = pending["id"]

    # 3. Approve
    r = client.post(f"/adm/kyc/{user_id}/approve", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()["payload"]
    assert body["kyc_status"] == "approved"
    assert body["is_active"] is True

    # 4. The exporter can now log in
    r = client.post("/exp/login", json={"email": "newtest@example.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["payload"]["token"]


def test_resend_approval_email_bypasses_dedupe(client, admin_token):
    """If the original activation email failed silently (SMTP outage), admin
    can re-fire it via /adm/users/{id}/resend-approval-email without being
    blocked by the dedupe key the first attempt wrote.
    """
    from app.database import SessionLocal
    from app.models import NotificationLog, User

    # Register + approve a fresh exporter
    r = client.put("/exp/register", data={
        "firstname": "Resend", "lastname": "Target", "phone": "+2348100009990",
        "email": "resend-target@example.com", "password": "password123",
        "profile_name": "resend-target",
    })
    assert r.status_code == 200, r.text
    # Stamp kyc_submitted_at - approve now requires the exporter to have
    # submitted for review first.
    from datetime import datetime, timezone
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "resend-target@example.com").first()
        u.kyc_submitted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        user_id = u.id

    r = client.post(f"/adm/kyc/{user_id}/approve", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text

    # The original approval already wrote an `activated:{id}` log row.
    with SessionLocal() as db:
        first_row = (
            db.query(NotificationLog)
            .filter(NotificationLog.dedupe_key == f"activated:{user_id}")
            .first()
        )
        assert first_row is not None

    # Admin force-resend - bypasses dedupe by passing no dedupe key
    r = client.post(
        f"/adm/users/{user_id}/resend-approval-email",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["sent"] is True

    # A fresh log row landed under the resend template; original dedupe row
    # is unchanged (we didn't pretend the failed one succeeded).
    with SessionLocal() as db:
        resend_row = (
            db.query(NotificationLog)
            .filter(NotificationLog.template == "account_activated_resend",
                    NotificationLog.user_id == user_id)
            .first()
        )
        assert resend_row is not None


def test_resend_approval_email_rejects_non_approved(client, admin_token):
    """Can't resend before KYC actually approved."""
    from app.database import SessionLocal
    from app.models import User
    r = client.put("/exp/register", data={
        "firstname": "Pending", "lastname": "Resend", "phone": "+2348100009991",
        "email": "pending-resend@example.com", "password": "password123",
        "profile_name": "pending-resend",
    })
    assert r.status_code == 200
    with SessionLocal() as db:
        user_id = db.query(User).filter(User.email == "pending-resend@example.com").first().id
    r = client.post(
        f"/adm/users/{user_id}/resend-approval-email",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400


def test_admin_can_create_market(client, admin_token):
    r = client.put("/adm/market", headers={"Authorization": f"Bearer {admin_token}"},
                   data={"name": "Test Market", "location": "Test City", "city": "Test"})
    assert r.status_code == 200
    assert r.json()["payload"]["name"] == "Test Market"
