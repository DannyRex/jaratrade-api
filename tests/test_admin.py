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


def test_admin_can_create_market(client, admin_token):
    r = client.put("/adm/market", headers={"Authorization": f"Bearer {admin_token}"},
                   data={"name": "Test Market", "location": "Test City", "city": "Test"})
    assert r.status_code == 200
    assert r.json()["payload"]["name"] == "Test Market"
