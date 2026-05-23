"""Order confirmation emails fire on payment success, not order creation.

User feedback: receiving an "order placed" email the moment Place Order is
clicked is misleading if the buyer never completes payment. Emails are now
deferred to the payment-verify path so they only fire for orders the buyer
actually paid for. Both `verify_pay` (browser returns from FLW) and the
FLW webhook handler trigger the same `_send_order_confirmation_emails`
helper, with dedupe keys preventing double-send.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import NotificationLog, Order, Payment, Product, User
from app.routers.importer import _send_order_confirmation_emails


def _place_order(client: TestClient, importer_token: str) -> dict:
    """Helper: add a product to cart and place the order. Returns
    {order_id, tx_ref-ish placeholder}. Does NOT pay."""
    headers = {"Authorization": f"Bearer {importer_token}"}
    r = client.get("/public/products", params={"len": 1})
    product_id = r.json()["payload"]["data"][0]["id"]
    moq_r = client.get(f"/public/products/{product_id}")
    moq = moq_r.json()["payload"]["min_order_quantity"] or 1

    r = client.post("/imp/cart", headers=headers, data={
        "product_id": product_id, "quantity": max(moq, 2),
    })
    assert r.status_code == 200, r.text
    cart_id = r.json()["payload"]["cart_id"]

    r = client.post("/imp/order", headers=headers, data={
        "cart_id": cart_id,
        "delivery_info": '{"address":"test","city":"London","country":"UK"}',
    })
    assert r.status_code == 200, r.text
    return r.json()["payload"]


def _email_count(db: Session, template: str, order_id: str) -> int:
    """Count NotificationLog entries for a specific (template, order)."""
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.template == template,
            NotificationLog.dedupe_key.like(f"{template}:{order_id}"),
        )
        .count()
    )


# ── No email at order-create time ──────────────────────────────────────────

def test_create_order_does_not_send_confirmation_emails(client: TestClient, importer_token):
    """Place an order without paying. The order-placed-buyer + order-
    received-seller emails must NOT have been sent yet."""
    payload = _place_order(client, importer_token)
    order_id = payload["order_id"]

    with SessionLocal() as db:
        buyer_emails = _email_count(db, "order_placed_buyer", order_id)
        seller_emails = _email_count(db, "order_received_seller", order_id)

    assert buyer_emails == 0, (
        "Place Order should NOT trigger the buyer's confirmation email - "
        "that fires only after payment succeeds."
    )
    assert seller_emails == 0, (
        "Place Order should NOT trigger the seller's notification email - "
        "that fires only after payment succeeds."
    )


# ── Helper sends both emails when called directly ──────────────────────────

def test_send_order_confirmation_emails_helper_writes_two_logs(client: TestClient, importer_token):
    """Calling _send_order_confirmation_emails directly (the unit-level
    seam used by both verify_pay and the webhook handler) produces exactly
    one buyer email + one seller email."""
    payload = _place_order(client, importer_token)
    order_id = payload["order_id"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        buyer = db.get(User, order.importer_id)
        _send_order_confirmation_emails(db, order, buyer)

        assert _email_count(db, "order_placed_buyer", order_id) == 1
        assert _email_count(db, "order_received_seller", order_id) == 1


# ── Idempotent: calling twice doesn't double-send ──────────────────────────

def test_send_order_confirmation_emails_is_idempotent(client: TestClient, importer_token):
    """Verify_pay and the webhook can both fire for the same payment if FLW
    is slow / the user lingers. Dedupe key on NotificationLog must prevent
    the second call from sending a second email."""
    payload = _place_order(client, importer_token)
    order_id = payload["order_id"]

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        buyer = db.get(User, order.importer_id)
        _send_order_confirmation_emails(db, order, buyer)
        _send_order_confirmation_emails(db, order, buyer)  # second call

        assert _email_count(db, "order_placed_buyer", order_id) == 1, (
            "Second call duplicated the buyer email - dedupe broken"
        )
        assert _email_count(db, "order_received_seller", order_id) == 1, (
            "Second call duplicated the seller email - dedupe broken"
        )
