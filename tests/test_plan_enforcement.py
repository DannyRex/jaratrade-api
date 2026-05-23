"""Plan-tier enforcement tests.

Premium card promises Unlimited stores / market locations / product listings
and a 1.5% commission. Free tier caps at 2 stores, 1 market location, 5
product listings and 2% commission. Without these checks a Free user gets
Premium features for free.

Each test below is a regression guard for a specific enforcement point.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import ExporterPlan, Market, Product, Store, User
from app.seed import SEED_EXPORTER_PASSWORD


def _exporter_session(client: TestClient) -> str:
    r = client.post("/exp/login", json={
        "email": "exporter@jaratrade.com",
        "password": SEED_EXPORTER_PASSWORD,
    })
    assert r.status_code == 200, r.text
    return r.json()["payload"]["token"]


def _exporter_user(db: Session) -> User:
    u = db.query(User).filter(User.email == "exporter@jaratrade.com").first()
    assert u is not None
    return u


def _free_plan(db: Session) -> ExporterPlan:
    p = db.query(ExporterPlan).filter(ExporterPlan.is_default == 1).first()
    assert p is not None, "Seed should include a default (Free) exporter plan"
    return p


def _premium_plan(db: Session) -> ExporterPlan:
    p = (
        db.query(ExporterPlan)
        .filter(ExporterPlan.is_default == 0, ExporterPlan.monthly_subscription_fee > 0)
        .first()
    )
    assert p is not None, "Seed should include a Premium exporter plan"
    return p


SEEDED_STORE_COUNT = 1
SEEDED_PRODUCT_COUNT = 4  # matches seed.py: Garri, Suya Spice, Stockfish, Plantain Chips


def _reset_seller_to_free(db: Session) -> None:
    """Ensure the seeded exporter is on the Free plan with no extra stores
    or products carried over from earlier tests. The seed creates 1 store +
    4 products, so we delete any rows beyond those baselines.

    Order matters: products are deleted before stores because Product.store_id
    is NOT NULL; deleting a store with attached products triggers a NULLify
    that violates the constraint. We also use ascending order_by so the
    seeded (oldest) rows are kept and the test-created extras are dropped.
    """
    user = _exporter_user(db)
    user.plan_id = None  # null = falls through to is_default plan

    # Products first - keep the oldest 4 (seeded), delete anything newer.
    products_oldest_first = (
        db.query(Product)
        .filter(Product.exporter_id == user.id)
        .order_by(Product.time_created.asc())
        .all()
    )
    for prod in products_oldest_first[SEEDED_PRODUCT_COUNT:]:
        db.delete(prod)
    db.flush()

    # Stores next - keep the oldest (seeded), delete anything newer.
    stores_oldest_first = (
        db.query(Store)
        .filter(Store.exporter_id == user.id)
        .order_by(Store.time_created.asc())
        .all()
    )
    for s in stores_oldest_first[SEEDED_STORE_COUNT:]:
        db.delete(s)
    db.commit()


# ── Plan ceilings ─────────────────────────────────────────────────────────

def test_free_tier_blocks_second_store(client: TestClient):
    """max_store=1 on Free. Seed already created store #1, so the 2nd
    store must be blocked. "Store" and "market location" are unified
    user-side: one shop per Free seller."""
    with SessionLocal() as db:
        _reset_seller_to_free(db)
        free = _free_plan(db)
        assert free.max_store == 1
        market_id = db.query(Market).first().id

    token = _exporter_session(client)
    headers = {"Authorization": f"Bearer {token}"}

    # 2nd store - should be rejected (Free is capped at 1).
    r = client.put("/exp/store", headers=headers,
                   data={"market_id": market_id, "address": "Stall 14"})
    assert r.status_code == 403, (
        f"Free tier exporter should be blocked at 2nd store; got {r.status_code}: {r.text}"
    )
    assert "store" in r.text.lower()
    assert "premium" in r.text.lower(), (
        "Error should nudge toward Premium upgrade"
    )


def test_free_tier_market_cap_is_defensive(client: TestClient):
    """max_market=1 on Free. With max_store also = 1, the store cap fires
    first - which is fine; market cap is a defensive guard for any future
    plan that allows multiple stores while still restricting geography.

    This test confirms that trying to expand to a 2nd market is blocked
    (we don't strictly care which guard fires, only that the request is
    rejected with a Premium nudge)."""
    with SessionLocal() as db:
        _reset_seller_to_free(db)
        free = _free_plan(db)
        assert free.max_market == 1
        markets = db.query(Market).limit(2).all()
        assert len(markets) >= 2, "Seed must have at least 2 markets for this test"
        second_market_id = markets[1].id

    token = _exporter_session(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.put("/exp/store", headers=headers,
                   data={"market_id": second_market_id, "address": "Stall 1"})
    assert r.status_code == 403, (
        f"Free tier exporter should be blocked from a 2nd market; got {r.status_code}: {r.text}"
    )
    assert "premium" in r.text.lower()


def test_free_tier_blocks_sixth_product(client: TestClient):
    """max_product=5 on Free. Seed creates 4 products. After reset we
    should be able to add a 5th, no further."""
    with SessionLocal() as db:
        _reset_seller_to_free(db)
        free = _free_plan(db)
        assert free.max_product == 5

        # Find the seeded store + category so we have a place to put new products.
        user = _exporter_user(db)
        store = db.query(Store).filter(Store.exporter_id == user.id).first()
        from app.models import Category
        category = db.query(Category).first()
        store_id, category_id = store.id, category.id

    token = _exporter_session(client)
    headers = {"Authorization": f"Bearer {token}"}

    def _create(name: str):
        return client.put("/exp/product", headers=headers, data={
            "product_name": name,
            "description": "test",
            "category_id": category_id,
            "store_id": store_id,
            "price": 1000,
            "min_order_quantity": 1,
        })

    # Baseline is 4 seeded products. Adding the 5th should succeed.
    r = _create("Plan-test 5th product")
    assert r.status_code == 200, f"5th product should be allowed: {r.text}"

    # 6th must be blocked.
    r = _create("Should be blocked")
    assert r.status_code == 403, (
        f"Free tier exporter should be blocked at 6th product; got {r.status_code}: {r.text}"
    )
    assert "product" in r.text.lower()


def test_premium_tier_has_no_ceilings(client: TestClient):
    """Same exporter, switch them to Premium, and the third store / second
    market / sixth product should all succeed."""
    with SessionLocal() as db:
        _reset_seller_to_free(db)
        user = _exporter_user(db)
        premium = _premium_plan(db)
        user.plan_id = premium.id
        db.commit()
        markets = db.query(Market).limit(2).all()
        first_market_id, second_market_id = markets[0].id, markets[1].id
        store = db.query(Store).filter(Store.exporter_id == user.id).first()
        from app.models import Category
        category_id = db.query(Category).first().id
        store_id = store.id

    token = _exporter_session(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Third store in a second market - both should pass on Premium.
    for addr, mkt in [("S-A", first_market_id), ("S-B", second_market_id), ("S-C", second_market_id)]:
        r = client.put("/exp/store", headers=headers, data={"market_id": mkt, "address": addr})
        assert r.status_code == 200, f"Premium store create {addr}: {r.text}"

    # 6 products in a row should all pass on Premium.
    for i in range(6):
        r = client.put("/exp/product", headers=headers, data={
            "product_name": f"Premium product {i}",
            "description": "test",
            "category_id": category_id,
            "store_id": store_id,
            "price": 1000,
            "min_order_quantity": 1,
        })
        assert r.status_code == 200, f"Premium product #{i+1}: {r.text}"


# ── Commission rate ───────────────────────────────────────────────────────

def test_order_platform_fee_uses_seller_plan_commission(client: TestClient, importer_token):
    """A Free-tier seller charges 2%; a Premium-tier seller charges 1.5%.
    Same buyer, same product price, the platform_fee on the resulting order
    should differ."""
    # 1. Seller on Free plan -> buyer places an order -> platform_fee == 2% of subtotal.
    with SessionLocal() as db:
        _reset_seller_to_free(db)
        user = _exporter_user(db)
        user.plan_id = None  # falls back to default Free plan
        db.commit()

    free_fee = _place_order_and_read_fee(client, importer_token)

    # 2. Seller on Premium plan -> repeat -> platform_fee == 1.5% of subtotal.
    with SessionLocal() as db:
        user = _exporter_user(db)
        premium = _premium_plan(db)
        user.plan_id = premium.id
        db.commit()

    premium_fee = _place_order_and_read_fee(client, importer_token)

    assert premium_fee < free_fee, (
        f"Premium seller should pay a lower platform fee than Free; "
        f"got free={free_fee}, premium={premium_fee}"
    )
    # And the ratio should be ~0.75 (1.5% / 2% = 0.75)
    ratio = premium_fee / free_fee
    assert 0.7 < ratio < 0.8, f"Expected ~0.75 ratio, got {ratio:.3f}"


def _place_order_and_read_fee(client: TestClient, importer_token: str) -> float:
    """Helper: add one of the seeded seller's products to the cart, place the
    order, and return its platform_fee (read from the order detail response,
    since /imp/order itself only returns ids + total)."""
    headers = {"Authorization": f"Bearer {importer_token}"}
    # Pick a product belonging to the seeded exporter.
    with SessionLocal() as db:
        user = _exporter_user(db)
        prod = db.query(Product).filter(Product.exporter_id == user.id).first()
        assert prod is not None
        product_id = prod.id
        moq = prod.min_order_quantity or 1

    r = client.post("/imp/cart", headers=headers, data={
        "product_id": product_id, "quantity": max(moq, 2),
    })
    assert r.status_code == 200, r.text
    cart_id = r.json()["payload"]["cart_id"]

    r = client.post("/imp/order", headers=headers, data={
        "cart_id": cart_id,
        "delivery_info": '{"address":"42 Test Lane","city":"London","country":"UK"}',
    })
    assert r.status_code == 200, r.text
    order_id = r.json()["payload"]["order_id"]

    # Read the persisted order to get platform_fee (the create response is
    # intentionally lean: just order_id, order_number, total).
    r = client.get(f"/imp/order/{order_id}", headers=headers)
    assert r.status_code == 200, r.text
    return float(r.json()["payload"]["platform_fee"])
