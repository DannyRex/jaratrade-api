"""Dispute / refund flow tests."""
from app.database import SessionLocal
from app.models import Order, Payment


def _place_paid_delivered_order(client, importer_token):
    """Helper: place an order, mark it delivered + payment successful in DB."""
    r = client.get("/public/products", params={"len": 1})
    pid = r.json()["payload"]["data"][0]["id"]
    client.post("/imp/cart", headers={"Authorization": f"Bearer {importer_token}"},
                data={"product_id": pid, "quantity": 2})
    cart_id = client.get("/imp/cart", headers={"Authorization": f"Bearer {importer_token}"}).json()["payload"]["data"][0]["id"]
    r = client.post("/imp/order", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"cart_id": cart_id, "delivery_info": '{"address":"London"}'})
    order_id = r.json()["payload"]["order_id"]

    # Mark delivered + create successful payment so refund path works
    with SessionLocal() as db:
        import json
        order = db.get(Order, order_id)
        order.status = "delivered"
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


def test_importer_raises_dispute(client, importer_token):
    order_id = _place_paid_delivered_order(client, importer_token)

    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "damaged", "description": "Boxes were crushed in transit; product unsaleable."})
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["status"] == "open"
    assert p["reason"] == "damaged"


def test_cannot_double_dispute(client, importer_token):
    order_id = _place_paid_delivered_order(client, importer_token)
    client.post(f"/imp/order/{order_id}/dispute",
                headers={"Authorization": f"Bearer {importer_token}"},
                data={"reason": "quality", "description": "Quality below promised."})
    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "quality", "description": "Quality below promised again."})
    assert r.status_code == 409


def test_invalid_reason_rejected(client, importer_token):
    order_id = _place_paid_delivered_order(client, importer_token)
    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "lost_pet", "description": "Some description here please."})
    assert r.status_code == 400


def test_admin_can_acknowledge_and_resolve_refund(client, importer_token, admin_token):
    order_id = _place_paid_delivered_order(client, importer_token)
    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "wrong_item", "description": "Got chillies instead of garri."})
    dispute_id = r.json()["payload"]["id"]

    # Acknowledge
    r = client.post(f"/adm/disputes/{dispute_id}/acknowledge",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["payload"]["status"] == "in_review"

    # Resolve with refund (Flutterwave dev-stub returns successful)
    r = client.post(f"/adm/disputes/{dispute_id}/resolve",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    data={"resolution": "refund", "refund_amount": "100.00", "admin_notes": "Approved."})
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["status"] == "resolved"
    assert p["resolution"] == "refund"
    assert p["refund_amount"] == "100.00"

    # Underlying order status should now be 'refunded'
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.status == "refunded"


def test_admin_can_reject(client, importer_token, admin_token):
    order_id = _place_paid_delivered_order(client, importer_token)
    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "other", "description": "Not specific."})
    dispute_id = r.json()["payload"]["id"]
    r = client.post(f"/adm/disputes/{dispute_id}/reject",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    data={"admin_notes": "Insufficient evidence."})
    assert r.status_code == 200
    assert r.json()["payload"]["status"] == "rejected"


def test_admin_queue_filters_by_status(client, admin_token):
    r = client.get("/adm/disputes", params={"status": "open"},
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()["payload"]["rows"]
    assert all(d["status"] == "open" for d in rows)


def test_importer_disputes_filter_by_status(client, importer_token):
    """Regression: /imp/disputes was ignoring its ?status= filter and
    returning all rows. The filter should now scope to the requested status."""
    order_id = _place_paid_delivered_order(client, importer_token)
    client.post(f"/imp/order/{order_id}/dispute",
                headers={"Authorization": f"Bearer {importer_token}"},
                data={"reason": "damaged", "description": "Boxes were crushed; product unsaleable."})

    r = client.get("/imp/disputes", params={"status": "open"},
                   headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200
    open_rows = r.json()["payload"]["rows"]
    assert len(open_rows) >= 1
    assert all(d["status"] == "open" for d in open_rows)

    r = client.get("/imp/disputes", params={"status": "resolved"},
                   headers={"Authorization": f"Bearer {importer_token}"})
    resolved_rows = r.json()["payload"]["rows"]
    assert all(d["status"] == "resolved" for d in resolved_rows)


def test_exporter_can_see_disputes_filed_against_them(client, importer_token, exporter_token):
    """v2.5.1: sellers got no visibility into disputes against their orders.
    /exp/disputes now returns disputes where this exporter is the named party."""
    order_id = _place_paid_delivered_order(client, importer_token)
    r = client.post(f"/imp/order/{order_id}/dispute",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"reason": "quality", "description": "Quality not as advertised."})
    assert r.status_code == 200, r.text
    dispute_id = r.json()["payload"]["id"]

    r = client.get("/exp/disputes", headers={"Authorization": f"Bearer {exporter_token}"})
    assert r.status_code == 200, r.text
    rows = r.json()["payload"]["rows"]
    assert any(d["id"] == dispute_id for d in rows)

    r = client.get(f"/exp/disputes/{dispute_id}", headers={"Authorization": f"Bearer {exporter_token}"})
    assert r.status_code == 200
    assert r.json()["payload"]["id"] == dispute_id


def test_exporter_disputes_role_gated(client, importer_token):
    """An importer hitting the exporter dispute endpoint should be rejected
    by the role guard, not silently see other people's disputes."""
    r = client.get("/exp/disputes", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code in (401, 403)
