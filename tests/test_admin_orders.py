"""Admin /adm/orders + /adm/orders/stats tests.

Covers:
  - GET /adm/orders returns enriched buyer/seller/payment/payout columns
  - Status filter narrows results
  - Search by order # / buyer email / seller business
  - Pagination + total counts
  - Stats endpoint groups by status and reports GMV / pending payouts / disputes
  - Detail endpoint includes items + payments + payouts
"""
import json
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import Order, Payment, User


def _place_paid_order(importer_token, client):
    """Buyer places an order + we mark it paid via direct DB write."""
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
        order.status = "paid"
        db.add(Payment(
            order_id=order.id,
            tx_ref=f"JARAADM{order.id[:6]}",
            amount=order.total,
            currency=order.currency,
            status="successful",
            provider="flutterwave",
            provider_payload=json.dumps({"id": "1"}),
        ))
        db.commit()
    return order_id


def test_admin_orders_list_returns_enriched_rows(client, admin_token, importer_token):
    order_id = _place_paid_order(importer_token, client)
    r = client.get("/adm/orders", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    rows = payload["rows"]
    assert payload["total_length"] >= 1
    row = next(r for r in rows if r["id"] == order_id)

    # Enriched fields exist
    assert row["buyer"]["email"] == "importer@jaratrade.com"
    assert row["seller"]["business_name"]  # Adaeze Foods Ltd
    assert row["items_count"] >= 1
    assert row["payment_status"] == "successful"
    assert row["payout_status"] is None  # no payout yet
    assert row["has_dispute"] is False
    assert row["status"] == "paid"


def test_admin_orders_filter_by_status(client, admin_token, importer_token):
    _place_paid_order(importer_token, client)
    r = client.get(
        "/adm/orders",
        params={"status": "paid"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert len(rows) >= 1
    assert all(row["status"] == "paid" for row in rows)


def test_admin_orders_filter_by_status_returns_empty_for_unmatched(client, admin_token):
    r = client.get(
        "/adm/orders",
        params={"status": "refunded"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["payload"]["total_length"] == 0


def test_admin_orders_search_by_buyer_email(client, admin_token, importer_token):
    _place_paid_order(importer_token, client)
    r = client.get(
        "/adm/orders",
        params={"q": "importer@jaratrade.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert len(rows) >= 1
    assert all(row["buyer"]["email"] == "importer@jaratrade.com" for row in rows)


def test_admin_orders_search_by_seller_business(client, admin_token, importer_token):
    _place_paid_order(importer_token, client)
    r = client.get(
        "/adm/orders",
        params={"q": "Adaeze"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert len(rows) >= 1
    assert all("Adaeze" in (row["seller"]["business_name"] or "") for row in rows)


def test_admin_orders_pagination(client, admin_token, importer_token):
    # Two orders to span two pages of len=1
    _place_paid_order(importer_token, client)
    _place_paid_order(importer_token, client)
    page0 = client.get(
        "/adm/orders",
        params={"len": 1, "p": 0},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    page1 = client.get(
        "/adm/orders",
        params={"len": 1, "p": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert page0.json()["payload"]["total_length"] >= 2
    assert len(page0.json()["payload"]["rows"]) == 1
    assert len(page1.json()["payload"]["rows"]) == 1
    assert page0.json()["payload"]["rows"][0]["id"] != page1.json()["payload"]["rows"][0]["id"]


def test_admin_orders_stats(client, admin_token, importer_token):
    _place_paid_order(importer_token, client)
    r = client.get("/adm/orders/stats", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["total_orders"] >= 1
    assert "paid" in p["by_status"]
    assert float(p["gmv"]) > 0
    assert p["pending_payouts"] >= 0
    assert p["open_disputes"] >= 0


def test_admin_order_detail_includes_items_payments_payouts(
    client, admin_token, importer_token
):
    order_id = _place_paid_order(importer_token, client)
    r = client.get(
        f"/adm/orders/{order_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["id"] == order_id
    assert len(p["items"]) >= 1
    assert len(p["payments"]) >= 1
    assert p["payments"][0]["status"] == "successful"
    assert p["buyer"]["email"] == "importer@jaratrade.com"
    assert p["seller"]["business_name"]


def test_admin_order_detail_404_for_unknown(client, admin_token):
    r = client.get(
        "/adm/orders/does-not-exist",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


def test_admin_orders_requires_admin(client, importer_token):
    r = client.get("/adm/orders", headers={"Authorization": f"Bearer {importer_token}"})
    # require_admin returns 401/403 - either signals "not allowed"
    assert r.status_code in (401, 403)
