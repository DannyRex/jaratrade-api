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
    r = client.post("/imp/cart/sync", headers={"Authorization": f"Bearer {importer_token}"},
                    json={"items": [{"product_id": pid, "quantity": 1} for pid in pids], "replace": True})
    assert r.status_code == 200
    assert len(r.json()["payload"]["items"]) == 2


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
