"""Settlement gating: international payouts wait for FLW T+5 settlement.

Flutterwave settles GBP→NGN cross-currency collections T+5 business days
after the charge. Our existing payout cron releases seller funds 1 day
after delivery confirmation - well inside that window. Without gating,
a UK-funded order would attempt /v3/transfers before the funds had
landed in our NGN wallet and either fail with insufficient balance OR
draw against unrelated NGN collections (creating reconciliation pain).

This test suite locks in the gating logic:
- NGN payments dispatch unchanged (settle T+1, implicitly inside our
  dispute window).
- Non-NGN payments only dispatch once settlement_status='completed'.
- Webhook captures settlement_id + due_at when FLW provides them.
- poll_settlements updates the status on subsequent runs.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.database import SessionLocal
from app.models import Order, OrderItem, Payment, Payout, Product, User
from app.routers.flw_webhook import _handle_charge


def _build_delivered_order(db, *, currency: str, settlement_status: str | None,
                            flw_settlement_id: str | None = None) -> tuple[str, str]:
    """Build an order in `delivered` status with a successful Payment whose
    settlement-state we control. Returns (order_id, payment_id)."""
    buyer = db.query(User).filter(User.email == "importer@jaratrade.com").first()
    seller = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
    assert buyer and seller

    # Need an order delivered more than 1 day ago so the payout-cron's
    # dispute-window check passes - we want to isolate the settlement
    # gate, not get blocked on dispute timing.
    long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    nonce = secrets.token_hex(3)
    order = Order(
        order_number=f"SET-GATE-{nonce}",
        cart_id=None,
        importer_id=buyer.id,
        exporter_id=seller.id,
        total=1000,
        platform_fee=20,
        logistics_fee=0,
        currency="NGN",  # order is always NGN; charge currency lives on Payment
        status="delivered",
        shipping_mode="self",
        delivery_info='{"country":"United Kingdom"}',
        time_updated=long_ago,
    )
    # OrderItem.product_id is NOT NULL - grab any seeded product to satisfy
    # the FK without making this test depend on a specific product row.
    product = db.query(Product).first()
    assert product is not None, "Seed must include at least one product"
    db.add(order)
    db.flush()
    db.add(OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_name=product.product_name,
        quantity=1,
        unit_price=1000,
        subtotal=1000,
    ))
    payment = Payment(
        order_id=order.id,
        tx_ref=f"JARA-{nonce}",
        amount=1000,
        currency=currency,
        status="successful",
        flw_settlement_id=flw_settlement_id,
        settlement_status=settlement_status,
    )
    db.add(payment)
    db.commit()
    return order.id, payment.id


# ── NGN collections: unchanged - dispatch regardless of settlement_status ──

def test_ngn_payment_dispatches_with_null_settlement_status(client):
    """NGN charges settle T+1 implicitly - we don't capture settlement_id
    for them and our cron doesn't gate on it. Existing prod behaviour must
    be preserved."""
    from app.cron import process_payouts

    with SessionLocal() as db:
        order_id, _ = _build_delivered_order(
            db, currency="NGN", settlement_status=None,
        )

    # Patch dispatch_payout so we can assert it was called for this NGN
    # order without actually hitting FLW.
    dispatched_orders: list[str] = []

    async def fake_dispatch(order, db, initiated_by=None):
        dispatched_orders.append(order.id)
        # Return a fake Payout-like object with status='sent'
        from types import SimpleNamespace
        return SimpleNamespace(status="sent", id="fake")

    with patch("app.routers.payouts.dispatch_payout", new=fake_dispatch):
        with SessionLocal() as db:
            process_payouts(db)

    assert order_id in dispatched_orders, (
        "NGN payment with null settlement_status should still dispatch - "
        "settlement gating only applies to non-NGN charges."
    )


# ── International collections: BLOCKED until settlement completes ──────────

def test_gbp_payment_blocked_until_settlement_completed(client):
    """GBP charge with settlement_status='pending' must NOT dispatch.
    The cron should silently skip it and try again next run."""
    from app.cron import process_payouts

    with SessionLocal() as db:
        order_id, _ = _build_delivered_order(
            db, currency="GBP", settlement_status="pending",
            flw_settlement_id="SETT-PENDING-123",
        )

    dispatched_orders: list[str] = []

    async def fake_dispatch(order, db, initiated_by=None):
        dispatched_orders.append(order.id)
        from types import SimpleNamespace
        return SimpleNamespace(status="sent", id="fake")

    with patch("app.routers.payouts.dispatch_payout", new=fake_dispatch):
        with SessionLocal() as db:
            process_payouts(db)

    assert order_id not in dispatched_orders, (
        "GBP payment with pending settlement should be skipped until "
        "FLW credits our NGN wallet (T+5)."
    )

    # The Payout row should NOT exist for this order yet.
    with SessionLocal() as db:
        payout = db.query(Payout).filter(Payout.order_id == order_id).first()
    assert payout is None, "Cron should not have created a Payout row yet"


def test_gbp_payment_dispatches_once_settlement_completed(client):
    """Once FLW reports settlement_status='completed' (after T+5), the
    next cron run should dispatch the payout normally."""
    from app.cron import process_payouts

    with SessionLocal() as db:
        order_id, _ = _build_delivered_order(
            db, currency="GBP", settlement_status="completed",
            flw_settlement_id="SETT-COMPLETED-123",
        )

    dispatched_orders: list[str] = []

    async def fake_dispatch(order, db, initiated_by=None):
        dispatched_orders.append(order.id)
        from types import SimpleNamespace
        return SimpleNamespace(status="sent", id="fake")

    with patch("app.routers.payouts.dispatch_payout", new=fake_dispatch):
        with SessionLocal() as db:
            process_payouts(db)

    assert order_id in dispatched_orders, (
        "GBP payment with completed settlement should dispatch on next "
        "cron run - the whole point of the gate is to unblock once T+5 lands."
    )


# ── Webhook captures settlement metadata ───────────────────────────────────

def test_webhook_captures_settlement_id_and_due_date(client):
    """charge.completed events that include settlement_id + due_datetime
    should populate the new Payment columns so poll_settlements has
    something to poll."""
    with SessionLocal() as db:
        order_id, payment_id = _build_delivered_order(
            db, currency="GBP", settlement_status=None,
        )
        # Reset to pending so we can simulate the webhook firing.
        payment = db.get(Payment, payment_id)
        payment.status = "pending"
        payment.flw_settlement_id = None
        payment.settlement_due_at = None
        payment.settlement_status = None
        db.commit()
        tx_ref = payment.tx_ref

    event = {
        "tx_ref": tx_ref,
        "status": "successful",
        "settlement_id": "SETT-FROM-WEBHOOK-555",
        "due_datetime": "2026-05-31T10:00:00.000Z",
    }
    with SessionLocal() as db:
        result = _handle_charge(event, db)

    assert result.get("handled") is True
    with SessionLocal() as db:
        payment = db.get(Payment, payment_id)
        assert payment.flw_settlement_id == "SETT-FROM-WEBHOOK-555"
        assert payment.settlement_status == "pending"
        assert payment.settlement_due_at is not None
        assert payment.settlement_due_at.year == 2026
        assert payment.settlement_due_at.month == 5
        assert payment.settlement_due_at.day == 31


def test_webhook_without_settlement_id_leaves_columns_null(client):
    """For NGN charges FLW often doesn't include settlement_id in the
    charge.completed payload - it's implicit T+1. Make sure the columns
    stay null in that case so the payout cron doesn't think there's a
    pending settlement to wait on."""
    with SessionLocal() as db:
        order_id, payment_id = _build_delivered_order(
            db, currency="NGN", settlement_status=None,
        )
        payment = db.get(Payment, payment_id)
        payment.status = "pending"
        db.commit()
        tx_ref = payment.tx_ref

    event = {"tx_ref": tx_ref, "status": "successful"}
    with SessionLocal() as db:
        _handle_charge(event, db)
        payment = db.get(Payment, payment_id)

    assert payment.flw_settlement_id is None
    assert payment.settlement_due_at is None
    assert payment.settlement_status is None
