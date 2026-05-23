"""FLW_CHARGE_CURRENCY flag: charge UK cards in GBP, not NGN.

UK-issuing banks routinely block NGN charges as exotic-currency / high-
risk, even when the FLW merchant has international cards enabled. Charging
the same merchant in GBP via fx_convert lifts UK card acceptance from
~35% to ~90% empirically. The order itself stays NGN; only the charge
denomination switches.

Default (flag unset) MUST be the existing NGN behaviour - prod relies on
this for Nigerian-issued cards which work natively in NGN. Test both
paths so a future refactor can't silently regress either.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.database import SessionLocal
from app.models import Order, OrderItem, Payment, User
from app.routers.importer import _resolve_charge_currency


def _make_ngn_order(db, total: float = 35000.0) -> Order:
    """Build a minimal Order in the DB so _resolve_charge_currency has a real
    row to read from (saves us from constructing a synthetic instance with
    every field)."""
    buyer = db.query(User).filter(User.email == "importer@jaratrade.com").first()
    assert buyer is not None
    order = Order(
        order_number=f"TEST-{int(total)}",
        cart_id=None,
        importer_id=buyer.id,
        exporter_id=None,
        total=total,
        platform_fee=round(total * 0.02, 2),
        logistics_fee=0,
        currency="NGN",
        status="pending",
        shipping_mode="self",
        logistics_id=None,
        delivery_info="{}",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


# ── Default behaviour (flag unset) ─────────────────────────────────────────

def test_resolve_charge_currency_defaults_to_order_currency(client):
    """With FLW_CHARGE_CURRENCY unset, charge currency == order currency.
    This is what prod has been doing since launch; must stay that way."""
    with SessionLocal() as db:
        order = _make_ngn_order(db, total=42000.0)
        amount, currency = _resolve_charge_currency(order)
        assert currency == "NGN"
        assert amount == 42000.0


def test_resolve_charge_currency_unset_passthrough_even_with_fx_available(client):
    """fx_convert returning a usable rate shouldn't matter when the env var
    is unset - we only convert when the operator opts in explicitly."""
    with SessionLocal() as db:
        order = _make_ngn_order(db, total=10000.0)
    # Even if fx_convert would return a number, default behaviour stays NGN.
    with patch("app.routers.importer.fx_convert", return_value=4.85):
        amount, currency = _resolve_charge_currency(order)
    assert currency == "NGN"
    assert amount == 10000.0


# ── GBP path (flag set) ───────────────────────────────────────────────────

def test_resolve_charge_currency_gbp_converts_with_buffer(client, monkeypatch):
    """When FLW_CHARGE_CURRENCY=GBP, the NGN order total gets fx-converted
    to GBP and a small buffer is applied. The buffer is what protects the
    seller's NGN settlement amount from FX drift between init and capture."""
    monkeypatch.setenv("FLW_CHARGE_CURRENCY", "GBP")
    monkeypatch.setenv("FLW_CHARGE_CURRENCY_BUFFER_PCT", "1.0")
    # Re-initialise the cached settings so the env var change takes effect.
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            order = _make_ngn_order(db, total=2_000_000.0)  # ₦2M
        # Pretend the FX feed says NGN→GBP = 1/2000 (i.e. 1 GBP = 2000 NGN).
        # So 2M NGN ≈ £1000 before buffer.
        with patch("app.routers.importer.fx_convert", return_value=1000.0):
            amount, currency = _resolve_charge_currency(order)
        assert currency == "GBP"
        # 1.0% buffer → 1010.00
        assert amount == pytest.approx(1010.0, abs=0.01)
    finally:
        get_settings.cache_clear()


def test_resolve_charge_currency_gbp_falls_back_when_fx_unavailable(client, monkeypatch):
    """If the FX feed is down, charge in the native currency rather than
    blocking checkout. A potentially-declined card is a better failure
    mode than a 500."""
    monkeypatch.setenv("FLW_CHARGE_CURRENCY", "GBP")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            order = _make_ngn_order(db, total=50000.0)
        with patch("app.routers.importer.fx_convert", return_value=None):
            amount, currency = _resolve_charge_currency(order)
        # Fell back to native because fx_convert was unavailable.
        assert currency == "NGN"
        assert amount == 50000.0
    finally:
        get_settings.cache_clear()


def test_resolve_charge_currency_same_target_as_order_skips_conversion(client, monkeypatch):
    """Order already in GBP + FLW_CHARGE_CURRENCY=GBP → no double-conversion."""
    monkeypatch.setenv("FLW_CHARGE_CURRENCY", "GBP")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            order = _make_ngn_order(db, total=500.0)
            order.currency = "GBP"
            db.commit()
            db.refresh(order)
        # fx_convert MUST NOT be called for same-currency. Patch it to
        # raise so we'd notice if the helper accidentally invokes it.
        with patch("app.routers.importer.fx_convert", side_effect=AssertionError("should not convert")):
            amount, currency = _resolve_charge_currency(order)
        assert currency == "GBP"
        assert amount == 500.0
    finally:
        get_settings.cache_clear()
