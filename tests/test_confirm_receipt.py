"""Tests for buyer confirm-receipt + exporter status-change email flow.

Covers:
  - POST /exp/update_order emails the importer + stamps time_updated
  - POST /imp/order/{id}/confirm-receipt requires status=delivered
  - Confirming receipt makes the order immediately eligible for payout
    (i.e. bypasses the 7-day dispute window)
  - Confirm is idempotent
"""
import json
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import NotificationLog, Order, Payment, User


def _create_paid_delivered_order(importer_token, client):
    """Build an order that's been paid + the exporter marked delivered NOW.

    Crucially we do NOT back-date time_updated - this is the case the
    7-day dispute window would normally block, but a buyer confirmation
    should release.
    """
    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    assert cart_r.status_code == 200, cart_r.text
    cart_id = cart_r.json()["payload"]["cart_id"]
    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    assert order_r.status_code == 200, order_r.text
    order_id = order_r.json()["payload"]["order_id"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.status = "delivered"
        # Fresh delivered timestamp - dispute window still open
        order.time_updated = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(Payment(
            order_id=order.id,
            tx_ref=f"JARACONF{order.id[:6]}",
            amount=order.total,
            currency=order.currency,
            status="successful",
            provider="flutterwave",
            provider_payload=json.dumps({"id": "999", "tx_ref": f"JARACONF{order.id[:6]}"}),
        ))
        db.commit()
    return order_id


# ───────────────────────── Exporter status-change email ─────────────────────────

def test_exporter_update_order_emails_importer_and_stamps_time(
    client, exporter_token, importer_token
):
    """When the exporter advances an order's status, the importer should
    get an email and order.time_updated should refresh."""
    # Buyer-side prep: cart + order + mark it paid so the exporter can transition.
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
        order.status = "paid"
        # Set time_updated to something old so we can confirm it changes.
        order.time_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
        db.commit()
        before_time = order.time_updated

    # paid -> confirmed is the first valid step in the strict status sequence.
    r = client.post(
        "/exp/update_order",
        headers={"Authorization": f"Bearer {exporter_token}"},
        data={"order_id": order_id, "status": "confirmed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["status"] == "confirmed"

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.status == "confirmed"
        assert order.time_updated > before_time
        # Notification log carries the dedupe key we used
        log = (
            db.query(NotificationLog)
            .filter(NotificationLog.dedupe_key == f"order_status:confirmed:{order_id}")
            .first()
        )
        assert log is not None
        assert log.template == "order_status_confirmed"


def test_exporter_invalid_status_transition_rejected(
    client, exporter_token, importer_token
):
    """Can't jump from 'pending' to 'delivered'."""
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

    # Default status is 'pending' which is not in _ALLOWED_STATUS_TRANSITIONS.
    r = client.post(
        "/exp/update_order",
        headers={"Authorization": f"Bearer {exporter_token}"},
        data={"order_id": order_id, "status": "delivered"},
    )
    assert r.status_code == 400


# ───────────────────────── Buyer confirm-receipt ─────────────────────────

def test_confirm_receipt_stamps_timestamp(client, importer_token):
    order_id = _create_paid_delivered_order(importer_token, client)
    r = client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["already_confirmed"] is False
    assert payload["confirmed_received_at"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.confirmed_received_at is not None


def test_confirm_receipt_idempotent(client, importer_token):
    order_id = _create_paid_delivered_order(importer_token, client)
    client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    r2 = client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["payload"]["already_confirmed"] is True


def test_confirm_receipt_rejected_when_not_delivered(client, importer_token):
    """Can't confirm something that's still 'paid' or 'shipped'."""
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
        order.status = "shipped"
        db.commit()

    r = client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    assert r.status_code == 400
    assert "delivered" in r.text.lower()


def test_confirm_receipt_blocked_for_other_importer(client, importer_token):
    """Importer A can't confirm Importer B's order."""
    order_id = _create_paid_delivered_order(importer_token, client)

    # Login as a separate importer (admin@jaratrade.com is admin; we can't
    # easily create a second importer here, so re-bind the order to a
    # nonexistent user via the DB and assert the auth check still 404s).
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.importer_id = "not-this-user"
        db.commit()
    try:
        r = client.post(
            f"/imp/order/{order_id}/confirm-receipt",
            headers={"Authorization": f"Bearer {importer_token}"},
        )
        assert r.status_code == 404
    finally:
        # Restore so other tests aren't affected
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            buyer = db.query(User).filter(User.email == "importer@jaratrade.com").first()
            order.importer_id = buyer.id
            db.commit()


# ───────────────────────── Early payout eligibility ─────────────────────────

def test_confirmed_receipt_makes_order_eligible_within_dispute_window(
    client, admin_token, importer_token
):
    """A delivered order whose time_updated is fresh (inside the 7-day
    window) is NOT eligible by default - but becomes eligible the moment
    the buyer confirms receipt."""
    order_id = _create_paid_delivered_order(importer_token, client)

    # Before confirmation: not eligible (within 7-day window)
    r = client.get("/adm/payouts/eligible", headers={"Authorization": f"Bearer {admin_token}"})
    rows = r.json()["payload"]["rows"]
    assert not any(row["order_id"] == order_id for row in rows)

    # Buyer confirms
    cr = client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    assert cr.status_code == 200, cr.text

    # After confirmation: eligible
    r2 = client.get("/adm/payouts/eligible", headers={"Authorization": f"Bearer {admin_token}"})
    rows2 = r2.json()["payload"]["rows"]
    assert any(row["order_id"] == order_id for row in rows2), (
        "Order should be eligible immediately after buyer confirms receipt"
    )


def test_confirmed_receipt_dispatch_payout_succeeds_within_window(
    client, admin_token, importer_token
):
    """The admin /send endpoint should accept a confirmed-receipt order
    even when the dispute window hasn't elapsed."""
    order_id = _create_paid_delivered_order(importer_token, client)
    client.post(
        f"/imp/order/{order_id}/confirm-receipt",
        headers={"Authorization": f"Bearer {importer_token}"},
    )
    r = client.post(
        f"/adm/payouts/{order_id}/send",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["status"] in ("sent", "pending", "completed")
