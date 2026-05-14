"""Stock tracking + inventory-confirmation tests."""
from app.database import SessionLocal
from app.models import Cart, Product, User


def _first_product_id(client):
    r = client.get("/public/products", params={"len": 1})
    return r.json()["payload"]["data"][0]["id"]


def _wipe_importer_carts():
    """Tests that depend on a clean cart state should call this first.
    Earlier tests in the run (disputes, cart_orders) leave 'ordered' carts
    behind; without cleanup the importer can end up with multiple carts and
    list_carts ordering becomes unreliable."""
    with SessionLocal() as db:
        importer = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        if importer:
            for c in db.query(Cart).filter(Cart.importer_id == importer.id).all():
                db.delete(c)
            db.commit()


def test_seeded_products_have_stock(client):
    r = client.get("/public/products", params={"len": 4})
    rows = r.json()["payload"]["data"]
    assert all(int(p["stock_quantity"]) > 0 for p in rows)


def test_order_decrements_stock_and_cancel_restores(client, importer_token, exporter_token):
    _wipe_importer_carts()
    pid = _first_product_id(client)

    # Pin stock to a known starting value so prior tests can't skew the math.
    with SessionLocal() as db:
        p = db.get(Product, pid)
        p.stock_quantity = 80
        db.commit()
        before = p.stock_quantity

    # Add 3 to cart -> create order -> stock should drop by 3.
    # Use cart_id returned from POST directly (don't read list_carts — it can
    # include stale "ordered" carts from prior tests).
    r = client.post("/imp/cart", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"product_id": pid, "quantity": 3})
    assert r.status_code == 200, r.text
    cart_id = r.json()["payload"]["cart_id"]

    r = client.post("/imp/order", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"cart_id": cart_id, "delivery_info": '{"address":"London"}'})
    assert r.status_code == 200, r.text
    order_id = r.json()["payload"]["order_id"]

    with SessionLocal() as db:
        after_order = db.get(Product, pid).stock_quantity
    assert after_order == before - 3

    # Cancel restores
    r = client.delete(f"/imp/order/{order_id}", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200

    with SessionLocal() as db:
        after_cancel = db.get(Product, pid).stock_quantity
    assert after_cancel == before


def test_order_blocked_when_insufficient_stock(client, importer_token):
    pid = _first_product_id(client)
    # Set stock to 1
    with SessionLocal() as db:
        p = db.get(Product, pid)
        p.stock_quantity = 1
        db.commit()

    r = client.post("/imp/cart", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"product_id": pid, "quantity": 5})
    cart_id = r.json()["payload"]["cart_id"]

    r = client.post("/imp/order", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"cart_id": cart_id, "delivery_info": "{}"})
    assert r.status_code == 409
    assert "Stock check failed" in r.text


def test_exporter_can_confirm_inventory(client, exporter_token):
    pid = _first_product_id(client)
    # Make it stale
    with SessionLocal() as db:
        from datetime import datetime, timedelta, timezone
        p = db.get(Product, pid)
        p.last_inventory_update_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        db.commit()

    r = client.post(f"/exp/product/{pid}/confirm-inventory",
                    headers={"Authorization": f"Bearer {exporter_token}"},
                    data={"stock_quantity": 50})
    assert r.status_code == 200
    assert r.json()["payload"]["stock_quantity"] == 50

    with SessionLocal() as db:
        p = db.get(Product, pid)
        assert p.stock_quantity == 50
        assert p.last_inventory_update_at is not None


def test_bulk_confirm_inventory(client, exporter_token):
    r = client.post("/exp/product/confirm-inventory-all",
                    headers={"Authorization": f"Bearer {exporter_token}"})
    assert r.status_code == 200
    assert r.json()["payload"]["confirmed"] >= 1
