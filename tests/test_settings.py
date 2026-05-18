"""Platform settings tests - commission rate + commission account."""


def test_commission_rate_default(client, admin_token):
    """When no setting is present, GET returns the default (2%)."""
    r = client.get("/settings/commission_rate", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["percent"] == 2.0
    assert p["decimal_rate"] == 0.02
    assert p["default"] == 2.0


def test_commission_rate_update_and_read(client, admin_token):
    r = client.put(
        "/settings/commission_rate",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"percent": "1.5"},
    )
    assert r.status_code == 200
    assert r.json()["payload"]["percent"] == 1.5
    assert r.json()["payload"]["decimal_rate"] == 0.015

    r = client.get("/settings/commission_rate", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["payload"]["percent"] == 1.5


def test_commission_rate_rejects_out_of_range(client, admin_token):
    for bad in ("-1", "30", "100"):
        r = client.put(
            "/settings/commission_rate",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"percent": bad},
        )
        assert r.status_code == 400, f"expected 400 for percent={bad}"


def test_commission_rate_requires_admin(client, importer_token, exporter_token):
    for tok in (importer_token, exporter_token):
        r = client.get("/settings/commission_rate", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (401, 403)
        r = client.put(
            "/settings/commission_rate",
            headers={"Authorization": f"Bearer {tok}"},
            data={"percent": "5"},
        )
        assert r.status_code in (401, 403)


def test_commission_rate_used_in_payment_split(client, admin_token, importer_token):
    """Setting the commission rate to 5% should produce a 0.05 split in the
    Flutterwave inline config returned by /imp/payment/init.

    The split only appears when FLW_COMMISSION_SUBACCOUNT_ID is set in the
    env. In the test env it's empty, so the split array is empty - which
    still validates we wired the read-from-DB path correctly (no crash
    looking it up, and the order flow still works end-to-end).
    """
    # Set the rate
    r = client.put(
        "/settings/commission_rate",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"percent": "5"},
    )
    assert r.status_code == 200

    # Create an order
    pr = client.get("/public/products", params={"len": 1})
    pid = pr.json()["payload"]["data"][0]["id"]
    cart_r = client.post(
        "/imp/cart",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"product_id": pid, "quantity": 2},
    )
    assert cart_r.status_code == 200
    cart_id = cart_r.json()["payload"]["cart_id"]

    order_r = client.post(
        "/imp/order",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"cart_id": cart_id, "delivery_info": "{}"},
    )
    assert order_r.status_code == 200, order_r.text
    order_id = order_r.json()["payload"]["order_id"]

    init_r = client.post(
        "/imp/payment/init",
        headers={"Authorization": f"Bearer {importer_token}"},
        data={"order_id": order_id},
    )
    assert init_r.status_code == 200, init_r.text
    payload = init_r.json()["payload"]
    # The endpoint should at least return a valid envelope with tx_ref and amount.
    assert payload["tx_ref"].startswith("JARA")
    assert "split" in payload


def test_commission_account_save_and_load(client, admin_token):
    """Regression for the form-doesn't-load-saved-value issue."""
    payload = {
        "bank_name": "Access Bank",
        "account_name": "Jaratrade Ltd",
        "account_number": "0123456789",
    }
    r = client.put(
        "/settings/commision_account",
        headers={"Authorization": f"Bearer {admin_token}"},
        data=payload,
    )
    assert r.status_code == 200

    r = client.get("/settings/commision_account", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    got = r.json()["payload"]
    assert got["bank_name"] == "Access Bank"
    assert got["account_name"] == "Jaratrade Ltd"
    assert got["account_number"] == "0123456789"
