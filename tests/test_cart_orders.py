"""Cart, order, and payment flow tests."""


def _add_one_product(client, token):
    """Helper: pick the first listed product and add it to the importer's cart."""
    r = client.get("/public/products", params={"len": 1})
    product_id = r.json()["payload"]["data"][0]["id"]
    r = client.post("/imp/cart", headers={"Authorization": f"Bearer {token}"},
                    data={"product_id": product_id, "quantity": 3})
    assert r.status_code == 200, r.text
    return r.json()["payload"]["cart_id"], product_id


def test_add_to_cart_returns_cart_id(client, importer_token):
    cart_id, _ = _add_one_product(client, importer_token)
    assert cart_id


def test_view_cart_includes_added_item(client, importer_token):
    cart_id, _ = _add_one_product(client, importer_token)
    r = client.get(f"/imp/cart/{cart_id}", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200
    assert any(i["quantity"] >= 1 for i in r.json()["payload"]["items"])


def test_cart_sync_replaces_items(client, importer_token):
    """POST /imp/cart/sync should replace the current cart with the supplied items."""
    r = client.get("/public/products", params={"len": 2})
    pids = [p["id"] for p in r.json()["payload"]["data"][:2]]
    # Quantity 2 because seeded products carry min_order_quantity=2 and the
    # sync endpoint enforces MOQ same as the explicit add-to-cart endpoint.
    r = client.post("/imp/cart/sync", headers={"Authorization": f"Bearer {importer_token}"},
                    json={"items": [{"product_id": pid, "quantity": 2} for pid in pids], "replace": True})
    assert r.status_code == 200
    assert len(r.json()["payload"]["items"]) == 2


def test_cart_sync_rejects_below_moq(client, importer_token):
    """Regression: /cart/sync used to skip the MOQ check that /cart enforces,
    letting buyers sneak a quantity-1 line item past the validation."""
    r = client.get("/public/products", params={"len": 1})
    pid = r.json()["payload"]["data"][0]["id"]
    r = client.post(
        "/imp/cart/sync",
        headers={"Authorization": f"Bearer {importer_token}"},
        json={"items": [{"product_id": pid, "quantity": 1}], "replace": True},
    )
    assert r.status_code == 400, r.text
    assert "minimum order quantity" in r.text.lower()


def test_create_order_rejects_below_moq_in_cart(client, importer_token):
    """Final-defence MOQ check in /imp/order. We construct a cart item
    directly in the DB at quantity=1 (bypassing both /cart and /cart/sync),
    then try to check out - the order endpoint should refuse.

    Self-contained: creates its own cart row, cleans up at the end so the
    orphaned quantity-1 line item doesn't pollute later tests' /imp/cart
    lookups."""
    from app.database import SessionLocal
    from app.models import Cart, CartItem, Product, User

    cart_id = None
    try:
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
            prod = db.query(Product).filter(Product.min_order_quantity > 1).first()
            assert prod is not None, "Seed should include at least one MOQ>1 product"
            cart = Cart(importer_id=user.id, status="probe")  # custom status so it isn't an active cart
            db.add(cart)
            db.flush()
            db.add(CartItem(
                cart_id=cart.id,
                product_id=prod.id,
                quantity=1,  # below MOQ
                unit="cartons",
                unit_price=prod.price,
                subtotal=float(prod.price),
            ))
            db.commit()
            cart_id = cart.id

        r = client.post(
            "/imp/order",
            headers={"Authorization": f"Bearer {importer_token}"},
            data={"cart_id": cart_id, "delivery_info": '{"address":"Test"}'},
        )
        assert r.status_code == 400, r.text
        assert "moq" in r.text.lower() or "minimum order quantity" in r.text.lower()
    finally:
        if cart_id:
            with SessionLocal() as db:
                c = db.get(Cart, cart_id)
                if c:
                    for it in list(c.items):
                        db.delete(it)
                    db.delete(c)
                    db.commit()


def test_create_order_then_init_payment(client, importer_token):
    cart_id, _ = _add_one_product(client, importer_token)
    # Create order
    r = client.post("/imp/order", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"cart_id": cart_id, "delivery_info": '{"address":"Test"}'})
    assert r.status_code == 200, r.text
    order_id = r.json()["payload"]["order_id"]

    # Init payment - returns Flutterwave Inline config
    r = client.post("/imp/payment/init", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"order_id": order_id})
    assert r.status_code == 200, r.text
    config = r.json()["payload"]
    assert "tx_ref" in config
    assert config["tx_ref"].startswith("JARA")


def test_other_users_cannot_see_each_others_orders(client, importer_token, exporter_token):
    cart_id, _ = _add_one_product(client, importer_token)
    r = client.post("/imp/order", headers={"Authorization": f"Bearer {importer_token}"},
                    data={"cart_id": cart_id, "delivery_info": "{}"})
    order_id = r.json()["payload"]["order_id"]

    # Exporter trying to fetch it through the importer endpoint should 403
    r = client.get(f"/imp/order/{order_id}", headers={"Authorization": f"Bearer {exporter_token}"})
    assert r.status_code in (401, 403)
