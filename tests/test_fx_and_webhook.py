"""FX rate config + Flutterwave webhook + payout cron tests (v3.6)."""
import json
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import Order, Payment, Payout, Setting, User


# ───────────────────────── FX rate config ─────────────────────────

def test_fx_rate_get_default(client, admin_token):
    r = client.get("/settings/fx_rate", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["from"] == "NGN"
    assert p["to"] == "GBP"
    assert p["effective_rate"] is not None
    assert p["override_rate"] is None


def test_fx_rate_update_then_used_in_conversion(client, admin_token):
    r = client.put(
        "/settings/fx_rate",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"from_currency": "NGN", "to_currency": "GBP", "rate": "0.0006"},
    )
    assert r.status_code == 200

    r = client.get("/settings/fx_rate", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["override_rate"] == 0.0006
    assert p["effective_rate"] == 0.0006

    # /public/products should now include the secondary_amount.
    r = client.get("/public/products", params={"len": 1})
    p = r.json()["payload"]["data"][0]
    assert p["secondary_currency"] == "GBP"
    # 18000 NGN * 0.0006 = 10.80
    assert p["secondary_amount"] is not None
    assert abs(float(p["secondary_amount"]) - 10.80) < 0.01


def test_fx_rate_delete_restores_default(client, admin_token):
    client.put(
        "/settings/fx_rate",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"from_currency": "NGN", "to_currency": "GBP", "rate": "0.0009"},
    )
    r = client.request(
        "DELETE",
        "/settings/fx_rate",
        params={"from_currency": "NGN", "to_currency": "GBP"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    r = client.get("/settings/fx_rate", headers={"Authorization": f"Bearer {admin_token}"})
    p = r.json()["payload"]
    assert p["override_rate"] is None


def test_fx_rate_requires_admin(client, importer_token):
    r = client.get("/settings/fx_rate", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code in (401, 403)
    r = client.put(
        "/settings/fx_rate",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"from_currency": "NGN", "to_currency": "GBP", "rate": "0.001"},
    )
    assert r.status_code in (401, 403)


def test_fx_rate_rejects_invalid_inputs(client, admin_token):
    for bad in ("0", "-1"):
        r = client.put(
            "/settings/fx_rate",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"from_currency": "NGN", "to_currency": "GBP", "rate": bad},
        )
        assert r.status_code == 400


def test_secondary_amount_omitted_when_listing_already_gbp(client):
    r = client.get("/public/products")
    rows = r.json()["payload"]["data"]
    # All seeded products are NGN; if any aren't they should have null secondary.
    for p in rows:
        if (p.get("currency") or "").upper() == "GBP":
            assert p.get("secondary_amount") in (None, "")


# ───────────────────────── Webhook ─────────────────────────

def test_webhook_rejects_when_secret_set_and_header_missing(client, monkeypatch):
    """When FLW_WEBHOOK_SECRET is configured, requests without a matching
    `verif-hash` header should 401."""
    from app.routers import flw_webhook as wh
    monkeypatch.setattr(wh.settings, "flw_webhook_secret", "shhh")

    r = client.post("/public/flutterwave/webhook", json={"event": "charge.completed", "data": {}})
    assert r.status_code == 401


def test_webhook_accepts_signed_charge_completed(client, importer_token, monkeypatch):
    """charge.completed for a known tx_ref should flip the Payment to
    'successful' and the order to 'paid'."""
    from app.routers import flw_webhook as wh
    monkeypatch.setattr(wh.settings, "flw_webhook_secret", "shhh")

    # Set up a pending payment we can resolve via webhook
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
    tx_ref = init_r.json()["payload"]["tx_ref"]

    body = {
        "event": "charge.completed",
        "data": {"tx_ref": tx_ref, "status": "successful", "amount": 1000, "currency": "NGN"},
    }
    r = client.post(
        "/public/flutterwave/webhook",
        json=body,
        headers={"verif-hash": "shhh"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["handled"] is True

    # The Payment should now be successful + Order=paid
    with SessionLocal() as db:
        p = db.query(Payment).filter(Payment.tx_ref == tx_ref).first()
        assert p.status == "successful"
        o = db.get(Order, order_id)
        assert o.status == "paid"


def test_webhook_transfer_completed_marks_payout(client, monkeypatch):
    """transfer.completed for a known payout reference should flip
    Payout.status from 'sent' -> 'completed'."""
    from app.routers import flw_webhook as wh
    monkeypatch.setattr(wh.settings, "flw_webhook_secret", "shhh")

    # Seed a Payout we can resolve
    with SessionLocal() as db:
        order = db.query(Order).first()
        seller = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        ref = "JARAPAYTEST123"
        # Clean stale
        db.query(Payout).filter(Payout.reference == ref).delete()
        db.add(Payout(
            order_id=order.id,
            seller_id=seller.id,
            amount=100.00,
            currency="NGN",
            reference=ref,
            status="sent",
            provider="flutterwave",
        ))
        db.commit()

    body = {
        "event": "transfer.completed",
        "data": {"reference": ref, "status": "SUCCESSFUL", "amount": 100, "currency": "NGN"},
    }
    r = client.post(
        "/public/flutterwave/webhook",
        json=body,
        headers={"verif-hash": "shhh"},
    )
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        p = db.query(Payout).filter(Payout.reference == ref).first()
        assert p.status == "completed"


def test_webhook_ignores_unknown_event(client, monkeypatch):
    from app.routers import flw_webhook as wh
    monkeypatch.setattr(wh.settings, "flw_webhook_secret", "shhh")

    r = client.post(
        "/public/flutterwave/webhook",
        json={"event": "subscription.something", "data": {}},
        headers={"verif-hash": "shhh"},
    )
    assert r.status_code == 200
    body = r.json()["payload"]
    assert body["handled"] is False


# ───────────────────────── Payout cron ─────────────────────────

def _setup_eligible_order(importer_token, client) -> str:
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
        order.time_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        db.add(Payment(
            order_id=order.id,
            tx_ref=f"JARA{order.id[:6]}",
            amount=order.total,
            currency=order.currency,
            status="successful",
            provider="flutterwave",
            provider_payload=json.dumps({"id": "12345"}),
        ))
        db.commit()
    return order_id


def test_process_payouts_cron_dispatches_eligible(client, importer_token):
    """Setting up one eligible order, then calling process_payouts(), should
    create a Payout record and return count >= 1."""
    from app.cron import process_payouts

    order_id = _setup_eligible_order(importer_token, client)

    with SessionLocal() as db:
        n = process_payouts(db)
    assert n >= 1

    with SessionLocal() as db:
        payout = db.query(Payout).filter(Payout.order_id == order_id).first()
        assert payout is not None
        assert payout.initiated_by == "cron"
        assert payout.status in ("sent", "completed")


def test_process_payouts_cron_idempotent(client, importer_token):
    """A second run with no new eligible orders should dispatch zero."""
    from app.cron import process_payouts

    _setup_eligible_order(importer_token, client)
    with SessionLocal() as db:
        process_payouts(db)
    # Second run shouldn't double-dispatch
    with SessionLocal() as db:
        n = process_payouts(db)
    assert n == 0
