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
