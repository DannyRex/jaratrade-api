"""Auth flow tests: login per role, role guard, registration, 2FA challenge."""
import pyotp


def test_login_importer_succeeds(client):
    r = client.post("/imp/login", json={"email": "importer@jaratrade.com", "password": "REDACTED-old-default"})
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
    r = client.post("/exp/login", json={"email": "importer@jaratrade.com", "password": "REDACTED-old-default"})
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
        # Exporters still gated on KYC before they can transact
        assert user.is_active is False
        assert user.kyc_status == "pending"
        # BusinessProfile not created at slim signup - that comes via the
        # profile-update endpoint once the exporter fills in business details
        assert user.business is None


def test_2fa_enroll_confirm_login_flow(client, importer_token):
    # 1. Enroll
    r = client.post("/auth/2fa/enroll", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200
    secret = r.json()["payload"]["secret"]

    # 2. Confirm with the right code
    code = pyotp.TOTP(secret).now()
    r = client.post("/auth/2fa/confirm", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"code": code})
    assert r.status_code == 200
    assert r.json()["payload"]["enabled"] is True

    # 3. Plain login now returns requires_2fa instead of a token
    r = client.post("/imp/login", json={"email": "importer@jaratrade.com", "password": "REDACTED-old-default"})
    assert r.status_code == 200
    body = r.json()["payload"]
    assert body.get("requires_2fa") is True
    assert "token" not in body

    # 4. Use /auth/2fa/login with code to actually log in
    code = pyotp.TOTP(secret).now()
    r = client.post("/auth/2fa/login", json={
        "email": "importer@jaratrade.com",
        "password": "REDACTED-old-default",
        "code": code,
    })
    assert r.status_code == 200
    assert r.json()["payload"]["token"]

    # 5. Disable 2FA so subsequent tests still work
    r = client.post("/auth/2fa/disable", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"password": "REDACTED-old-default"})
    assert r.status_code == 200
