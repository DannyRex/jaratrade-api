"""payment_options narrows to card-only for UK buyers.

FLW confirmed UK bank-transfer isn't supported (the virtual account is on
a Nigerian MFB which UK remittance services can't target). Showing UK
buyers a bank-transfer tab they can't actually use is worse UX than
hiding it. The card-only restriction applies based on either the order's
delivery country or the user's registered country.
"""
from __future__ import annotations

import json
import secrets

import pytest

from app.database import SessionLocal
from app.models import Order, User
from app.routers.importer import _payment_options_for_buyer


def _make_order(db, *, delivery_country: str | None) -> Order:
    user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
    assert user is not None
    delivery = {"address": "1 test st", "city": "Anywhere"}
    if delivery_country is not None:
        delivery["country"] = delivery_country
    # order_number is UNIQUE - parametrize tests reuse the same delivery
    # country string, so suffix with a fresh nonce to avoid collisions.
    nonce = secrets.token_hex(3)
    order = Order(
        order_number=f"PMT-OPT-{delivery_country or 'none'}-{nonce}",
        cart_id=None,
        importer_id=user.id,
        exporter_id=None,
        total=10000,
        platform_fee=200,
        logistics_fee=0,
        currency="NGN",
        status="pending",
        shipping_mode="self",
        delivery_info=json.dumps(delivery),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


# ── UK-shipping orders → card only ─────────────────────────────────────────

@pytest.mark.parametrize("country", [
    "United Kingdom", "UNITED KINGDOM", "uk", "GB", "Great Britain",
    "england", "Scotland", "Wales", "Northern Ireland",
])
def test_uk_delivery_locks_to_card_only(client, country):
    with SessionLocal() as db:
        order = _make_order(db, delivery_country=country)
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        result = _payment_options_for_buyer(user, order)
    assert result == "card", (
        f"UK delivery (country={country!r}) should restrict to card-only, "
        f"got {result!r}"
    )


# ── Non-UK delivery → full rails ───────────────────────────────────────────

@pytest.mark.parametrize("country", ["Nigeria", "Ghana", "South Africa", "USA"])
def test_non_uk_delivery_keeps_all_rails(client, country):
    with SessionLocal() as db:
        order = _make_order(db, delivery_country=country)
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        result = _payment_options_for_buyer(user, order)
    assert result == "card,banktransfer,ussd"


# ── Delivery country missing falls back to user.country ────────────────────

def test_falls_back_to_user_country_when_delivery_country_missing(client):
    """If the delivery_info JSON has no country, use the user's registered
    country. This catches dev cases where shipping address was incomplete
    AND covers the moment between signup and shipping-address-setup where
    the only country signal is on the user row."""
    with SessionLocal() as db:
        order = _make_order(db, delivery_country=None)
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        # Seeded importer is registered in UK
        assert user.country and "kingdom" in (user.country or "").lower()
        result = _payment_options_for_buyer(user, order)
    assert result == "card"


def test_no_country_anywhere_defaults_to_full_rails(client):
    """Belt-and-suspenders: if neither delivery nor user.country resolves
    to a card-only country, allow all rails. Better to show too many
    payment methods than too few."""
    with SessionLocal() as db:
        order = _make_order(db, delivery_country=None)
        # Build a one-off user with no country to isolate the path
        original_country = None
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        original_country = user.country
        user.country = None
        db.commit()
        try:
            result = _payment_options_for_buyer(user, order)
            assert result == "card,banktransfer,ussd"
        finally:
            user.country = original_country
            db.commit()


# ── Malformed delivery_info doesn't crash ──────────────────────────────────

def test_malformed_delivery_info_is_safe(client):
    """Old orders with raw-string delivery_info shouldn't blow up the
    payment-options resolver."""
    with SessionLocal() as db:
        order = _make_order(db, delivery_country="Nigeria")
        order.delivery_info = "not valid json"
        db.commit()
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        result = _payment_options_for_buyer(user, order)
    # Falls back through to user.country which is UK on the seed importer
    assert result == "card"
