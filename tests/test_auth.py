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
