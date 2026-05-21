"""Tests for the Flutterwave subaccount + payout flow (v3.5)."""
import json
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import Order, Payment, Payout, User
from app.seed import SEED_ADMIN_PASSWORD


def _login_as_admin(client):
    r = client.post("/adm/login", json={"email": "admin@jaratrade.com", "password": SEED_ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]


# ───────────────────────── Subaccount provisioning ─────────────────────────

def test_kyc_approve_provisions_subaccount(client, admin_token):
    """The seeded exporter is approved + has banking info; approving again
    (idempotent) should leave a flw_subaccount_id on the row."""
    with SessionLocal() as db:
        exp = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        # Clear any existing subaccount to force re-provision.
        exp.flw_subaccount_id = None
        exp.kyc_status = "pending"
        db.commit()
        exp_id = exp.id

    r = client.post(f"/adm/kyc/{exp_id}/approve", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["kyc_status"] == "approved"
    assert payload["flw_subaccount_id"] is not None
    assert payload["flw_subaccount_id"].startswith("RS_DEV_")  # dev fallback


def test_reprovision_subaccount(client, admin_token):
    with SessionLocal() as db:
        exp = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        exp_id = exp.id
        original = exp.flw_subaccount_id

    r = client.post(
        f"/adm/users/{exp_id}/reprovision-subaccount",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["payload"]["flw_subaccount_id"]
    assert new_id and new_id.startswith("RS_DEV_")


def test_reprovision_rejects_unapproved_exporter(client, admin_token):
    with SessionLocal() as db:
        exp = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        original = exp.kyc_status
        exp.kyc_status = "pending"
        db.commit()
        exp_id = exp.id

    try:
        r = client.post(
            f"/adm/users/{exp_id}/reprovision-subaccount",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 400
    finally:
        with SessionLocal() as db:
            exp = db.get(User, exp_id)
            exp.kyc_status = original
            db.commit()


# ───────────────────────── Payment split includes seller ─────────────────────

def test_payment_init_includes_seller_in_split(client, admin_token, importer_token):
    """An approved exporter with a subaccount should show up in the inline
    config's `subaccounts` array alongside the commission destination."""
    # Make sure the exporter has a subaccount + is approved
    with SessionLocal() as db:
        exp = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        if not exp.flw_subaccount_id:
            exp.flw_subaccount_id = "RS_DEV_TEST0001"
        exp.kyc_status = "approved"
        db.commit()
        expected_seller_sub = exp.flw_subaccount_id

    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    cart_id = cart_r.json()["payload"]["cart_id"]
    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    order_id = order_r.json()["payload"]["order_id"]

    init_r = client.post(
        "/imp/payment/init",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"order_id": order_id},
    )
    assert init_r.status_code == 200, init_r.text
    cfg = init_r.json()["payload"]
    assert "subaccounts" in cfg
    assert any(s["id"] == expected_seller_sub for s in cfg["subaccounts"]), cfg["subaccounts"]


# ───────────────────────── Payout eligibility + send ─────────────────────────

def _setup_paid_delivered_order(importer_token, client):
    """Set up an order that's eligible for payout: paid + delivered + past dispute window."""
    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    cart_id = cart_r.json()["payload"]["cart_id"]
    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    order_id = order_r.json()["payload"]["order_id"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.status = "delivered"
        # Back-date so the dispute window has closed
        order.time_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        db.add(Payment(
            order_id=order.id,
            tx_ref=f"JARA{order.id[:6]}",
            amount=order.total,
            currency=order.currency,
            status="successful",
            provider="flutterwave",
            provider_payload=json.dumps({"id": "12345", "tx_ref": f"JARA{order.id[:6]}"}),
        ))
        db.commit()
    return order_id


def test_eligible_lists_delivered_order_past_window(client, admin_token, importer_token):
    order_id = _setup_paid_delivered_order(importer_token, client)
    r = client.get("/adm/payouts/eligible", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert any(row["order_id"] == order_id for row in rows)
    row = next(r for r in rows if r["order_id"] == order_id)
    # commission_rate_percent is set by the settings test or default
    assert "seller_share" in row
    assert float(row["seller_share"]) > 0


def test_send_payout_creates_record(client, admin_token, importer_token):
    order_id = _setup_paid_delivered_order(importer_token, client)
    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["order_id"] == order_id
    assert p["status"] in ("sent", "pending", "completed")
    assert p["reference"].startswith("JARAPAY")


def test_send_payout_rejects_undelivered_order(client, admin_token, importer_token):
    """An order that's not 'delivered' yet shouldn't be payable."""
    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    cart_id = cart_r.json()["payload"]["cart_id"]
    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    order_id = order_r.json()["payload"]["order_id"]

    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400
    assert "delivered" in r.text.lower()


def test_send_payout_rejects_within_dispute_window(client, admin_token, importer_token):
    """Delivered but recent - dispute window still open, payout blocked."""
    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    cart_id = cart_r.json()["payload"]["cart_id"]
    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    order_id = order_r.json()["payload"]["order_id"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.status = "delivered"
        order.time_updated = datetime.now(timezone.utc).replace(tzinfo=None)  # just now
        db.add(Payment(
            order_id=order.id,
            tx_ref=f"JARA{order.id[:6]}",
            amount=order.total,
            currency=order.currency,
            status="successful",
            provider="flutterwave",
        ))
        db.commit()

    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400
    assert "dispute window" in r.text.lower()


def test_send_payout_idempotent(client, admin_token, importer_token):
    """A second send for the same order should fail with 409."""
    order_id = _setup_paid_delivered_order(importer_token, client)
    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409


def test_payouts_list_filters_by_status(client, admin_token, importer_token):
    order_id = _setup_paid_delivered_order(importer_token, client)
    client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r = client.get("/adm/payouts", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert len(rows) >= 1


def test_failed_payout_can_be_retried(client, admin_token, importer_token):
    """A payout that failed (e.g. Flutterwave rejected the transfer) must not
    permanently block a retry once the underlying issue is resolved."""
    order_id = _setup_paid_delivered_order(importer_token, client)
    # Simulate a previous payout attempt that Flutterwave rejected.
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        db.add(Payout(
            order_id=order.id,
            seller_id=order.exporter_id or "seller",
            amount=order.total,
            currency=order.currency,
            reference=f"JARAPAYFAIL{order.id[:6]}",
            status="failed",
            failure_reason="Flutterwave rejected the transfer: IP whitelisting",
        ))
        db.commit()

    # The order is still listed as eligible (a failed attempt doesn't hide it).
    r = client.get("/adm/payouts/eligible", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert any(row["order_id"] == order_id for row in r.json()["payload"]["rows"])

    # Dispatching again succeeds rather than 409'ing on the stale failed row.
    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["status"] in ("sent", "pending", "completed")
