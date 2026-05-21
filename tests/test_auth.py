"""Auth flow tests: login per role, role guard, registration."""
from app.seed import SEED_IMPORTER_PASSWORD


def test_login_importer_succeeds(client):
    r = client.post("/imp/login", json={"email": "importer@jaratrade.com", "password": SEED_IMPORTER_PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] is True
    assert body["payload"]["token"]
    assert body["payload"]["firstname"] == "Tunde"


def test_login_wrong_password_401(client):
    r = client.post("/imp/login", json={"email": "importer@jaratrade.com", "password": "wrong"})
    assert r.status_code == 401


def test_login_wrong_role_403(client):
    # importer credentials sent to /exp/login should fail with 403
    r = client.post("/exp/login", json={"email": "importer@jaratrade.com", "password": SEED_IMPORTER_PASSWORD})
    assert r.status_code == 403


def test_protected_endpoint_requires_auth(client):
    r = client.get("/imp/profile")
    assert r.status_code == 401


def test_admin_only_endpoint_blocks_importer(client, importer_token):
    r = client.get("/adm/market", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 403


def test_register_importer_creates_account(client):
    r = client.put("/imp/register", data={
        "type": "individual",
        "firstname": "Test", "lastname": "User", "phone": "+447400000777",
        "email": "test-user@example.com", "password": "password123",
        "profile_name": "test-user", "address": "London", "dob": "1990-01-01",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] is True


def test_register_importer_slim_signup_no_address_or_dob(client):
    """Regression: the v3.7 slim signup form only sends the six essentials -
    address and DOB move to the post-signup profile flow. The API must accept
    that shape and create the account anyway."""
    r = client.put("/imp/register", data={
        "type": "individual",
        "firstname": "Slim", "lastname": "User", "phone": "+447400000888",
        "email": "slim-importer@example.com", "password": "password123",
        "profile_name": "slim-user",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] is True


def test_login_blocks_unverified_user_and_resends_link(client):
    """A user that signed up but never verified should be told to verify,
    not handed a session token."""
    email = "unverified-login@example.com"
    r = client.put("/imp/register", data={
        "type": "individual", "firstname": "Verif", "lastname": "Pending",
        "phone": "+447400022001", "email": email, "password": "password123",
        "profile_name": "verif-pending",
    })
    assert r.status_code == 200, r.text

    r = client.post("/imp/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200
    payload = r.json()["payload"]
    # No token issued
    assert "token" not in payload
    # Flag for the frontend
    assert payload.get("requires_verification") is True
    assert payload.get("email") == email
    assert payload.get("role") == "importer"


def test_resignup_with_unverified_email_resends_verification(client):
    """Hitting register a second time with an email tied to an unverified
    account should resend the link, not block the user with a hard 409 and
    no recovery path."""
    email = "resignup@example.com"
    r = client.put("/imp/register", data={
        "type": "individual", "firstname": "Re", "lastname": "Signup",
        "phone": "+447400022002", "email": email, "password": "password123",
        "profile_name": "re-signup",
    })
    assert r.status_code == 200

    r2 = client.put("/imp/register", data={
        "type": "individual", "firstname": "Re", "lastname": "Signup",
        "phone": "+447400022003", "email": email, "password": "password123",
        "profile_name": "re-signup-2",
    })
    # Still rejects as a duplicate (the existing user wasn't replaced) but
    # the message tells the user a fresh link was sent.
    assert r2.status_code == 409
    assert "verification" in r2.text.lower()


def test_login_works_normally_for_verified_user(client):
    """Sanity: the seeded importer is pre-verified and should log in
    cleanly with a token, no requires_verification flag."""
    from app.seed import SEED_IMPORTER_PASSWORD
    r = client.post(
        "/imp/login",
        json={"email": "importer@jaratrade.com", "password": SEED_IMPORTER_PASSWORD},
    )
    assert r.status_code == 200
    payload = r.json()["payload"]
    assert payload.get("token")
    assert "requires_verification" not in payload


def test_register_exporter_slim_signup_no_business_or_address(client):
    """Same as above but for exporters - business details + KYC docs all
    move to the post-signup profile flow. The User row should be created,
    the BusinessProfile row deferred until the exporter fills it in."""
    from app.database import SessionLocal
    from app.models import User

    r = client.put("/exp/register", data={
        "firstname": "Slim", "lastname": "Exporter", "phone": "+2348100000777",
        "email": "slim-exporter@example.com", "password": "password123",
        "profile_name": "slim-exp",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] is True

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "slim-exporter@example.com").first()
        assert user is not None
        # is_active is True from signup so the exporter can log in and
        # complete their profile. KYC gating is via kyc_status, not is_active.
        assert user.is_active is True
        assert user.kyc_status == "pending"
        assert user.kyc_submitted_at is None  # hasn't submitted for review
        # BusinessProfile not created at slim signup - that comes via the
        # profile-update endpoint once the exporter fills in business details
        assert user.business is None
