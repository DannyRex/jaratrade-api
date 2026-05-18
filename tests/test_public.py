"""Tests for public/unauthenticated endpoints."""

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] is True


def test_home_aggregate(client):
    r = client.get("/public")
    assert r.status_code == 200
    p = r.json()["payload"]
    # Seed creates 1 demo exporter, 4 products, 5 categories
    assert len(p["top_exporter"]) >= 1
    assert len(p["top_products"]) >= 4
    assert len(p["top_categories"]) >= 1


def test_products_listing_pagination(client):
    r = client.get("/public/products", params={"len": 2})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert len(p["data"]) <= 2
    assert p["meta"]["paging"]["len"] == 2


def test_reference_data_endpoints(client):
    for path in ("/public/data/category", "/public/data/market", "/public/data/bank",
                 "/public/data/logistics", "/public/data/importer_plan", "/public/data/exporter_plan"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert r.json()["status"] is True
        assert "rows" in r.json()["payload"]


def test_unknown_product_404(client):
    r = client.get("/public/products/does-not-exist")
    assert r.status_code == 404


def test_public_metrics(client):
    """Marketing-page metrics should return live, non-negative counts for
    each surface displayed on the homepage hero."""
    r = client.get("/public/metrics")
    assert r.status_code == 200
    p = r.json()["payload"]
    for key in ("verified_exporters", "active_skus", "markets", "categories"):
        assert key in p, f"missing {key}"
        assert isinstance(p[key], int) and p[key] >= 0
    # Seed creates at least one approved exporter, several products, 12 markets
    assert p["verified_exporters"] >= 1
    assert p["active_skus"] >= 1
    assert p["markets"] >= 1
    assert p["categories"] >= 1


def test_unverified_exporter_products_hidden_publicly(client):
    """Regression: products from a not-yet-approved exporter must not show
    up in /public/products, /public (home aggregate), or top_exporter."""
    from app.database import SessionLocal
    from app.models import Product, User

    with SessionLocal() as db:
        exp = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
        original_status = exp.kyc_status
        exp.kyc_status = "pending"
        db.commit()

        try:
            r = client.get("/public/products")
            assert r.status_code == 200
            assert r.json()["payload"]["data"] == []

            r = client.get("/public")
            assert r.status_code == 200
            p = r.json()["payload"]
            assert p["top_products"] == []
            assert all(e["id"] != exp.id for e in p["top_exporter"])
        finally:
            db.merge(User(id=exp.id, kyc_status=original_status))
            db.commit()


def test_verified_exporter_serializer_includes_kyc_flag(client):
    r = client.get("/public")
    assert r.status_code == 200
    for e in r.json()["payload"]["top_exporter"]:
        assert e["is_verified"] is True
        assert e["kyc_status"] == "approved"


def test_sort_price_asc_actually_orders_by_price(client):
    """Regression: freshness ranking was overriding the user's price sort."""
    r = client.get("/public/products", params={"sort_by": "price_asc"})
    assert r.status_code == 200
    rows = r.json()["payload"]["data"]
    prices = [float(p["price"]) for p in rows]
    assert prices == sorted(prices), f"price_asc was not sorted: {prices}"
