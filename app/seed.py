"""Idempotent seed of reference data + a demo admin/exporter/importer."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .constants import ROLE_ADMIN, ROLE_EXPORTER, ROLE_IMPORTER
from .models import (
    Bank,
    Category,
    ExporterPlan,
    ImporterPlan,
    LogisticsCompany,
    Market,
    Product,
    Setting,
    Store,
    User,
    BusinessProfile,
)
from .security import hash_password


def seed_default_data(db: Session) -> None:
    _seed_categories(db)
    _seed_markets(db)
    _seed_banks(db)
    _seed_logistics(db)
    _seed_plans(db)
    _seed_settings(db)
    _seed_demo_users(db)
    db.commit()


def _seed_categories(db: Session) -> None:
    if db.query(Category).count() > 0:
        return
    for name, desc in [
        ("Food & Beverage", "Edible goods - packaged foods, drinks, sauces"),
        ("Personal Care", "Cosmetics, hygiene, hair care"),
        ("Textiles", "Ankara, fabrics, apparel"),
        ("Spices & Condiments", "Pepper, suya seasoning, locust beans"),
        ("Snacks & Confectionery", "Chin chin, plantain chips, sweets"),
    ]:
        db.add(Category(name=name, description=desc, is_featured=1))


def _seed_markets(db: Session) -> None:
    if db.query(Market).count() > 0:
        return
    for name, location, lga, city, state in [
        ("Alaba International Market", "Ojo, Lagos", "Ojo", "Lagos", "Lagos"),
        ("Mushin Market", "Mushin, Lagos", "Mushin", "Lagos", "Lagos"),
        ("Balogun Market", "Lagos Island", "Lagos Island", "Lagos", "Lagos"),
        ("Oke Arin Market", "Lagos Island", "Lagos Island", "Lagos", "Lagos"),
        ("Ariaria International Market", "Aba, Abia", "Aba South", "Aba", "Abia"),
        ("Singer Market", "Kano", "Kano Municipal", "Kano", "Kano"),
        ("Dawanau Food Market", "Kano", "Dawakin Tofa", "Kano", "Kano"),
        ("Bodija Market", "Ibadan", "Ibadan North", "Ibadan", "Oyo"),
        ("Oil Mill Market", "Port Harcourt", "Obio-Akpor", "Port Harcourt", "Rivers"),
        ("Onitsha Main Market", "Onitsha, Anambra", "Onitsha North", "Onitsha", "Anambra"),
        ("Zaki Biam Yam Market", "Benue", "Ukum", "Zaki Biam", "Benue"),
    ]:
        db.add(Market(name=name, location=location, lga=lga, city=city, state=state, country="Nigeria"))


def _seed_banks(db: Session) -> None:
    if db.query(Bank).count() > 0:
        return
    for name, code in [
        ("Access Bank", "044"),
        ("First Bank of Nigeria", "011"),
        ("GTBank", "058"),
        ("UBA", "033"),
        ("Zenith Bank", "057"),
        ("Sterling Bank", "232"),
        ("Wema Bank", "035"),
        ("Fidelity Bank", "070"),
    ]:
        db.add(Bank(name=name, country="Nigeria", paystack_code=code, flutter_code=code))


def _seed_logistics(db: Session) -> None:
    if db.query(LogisticsCompany).count() > 0:
        return
    for name, desc in [
        ("DHL Africa Express", "Express international shipping with tracking"),
        ("Aramex Nigeria", "Affordable Africa-Europe shipping"),
        ("Red Star Express", "Cargo and last-mile delivery"),
        ("GIG Logistics", "Domestic + select international routes"),
    ]:
        db.add(LogisticsCompany(name=name, description=desc, email=f"hello@{name.lower().replace(' ', '')}.com", phone="+2348100000000"))


def _seed_plans(db: Session) -> None:
    if db.query(ImporterPlan).count() == 0:
        db.add(ImporterPlan(
            title="Free Tier",
            description="Standard access - capped at £2,000/month transactions and 48-hour support response.",
            monthly_subscription_fee=0,
            transaction_limit=2000,
            commission_percent=2,
            currency="GBP",
            is_default=1,
        ))
        db.add(ImporterPlan(
            title="Premium",
            description="Unlimited transactions, 12-hour priority support, early access to new listings, custom restock alerts.",
            monthly_subscription_fee=150,
            transaction_limit=-1,
            commission_percent=1.5,
            currency="GBP",
            is_default=0,
        ))
    if db.query(ExporterPlan).count() == 0:
        db.add(ExporterPlan(
            title="Free Tier",
            description="Up to 2 stores and 5 product listings. 2% commission per transaction.",
            monthly_subscription_fee=0,
            transaction_limit=-1,
            commission_percent=2,
            max_store=2,
            max_product=5,
            max_market=1,
            product_promotion=0,
            currency="NGN",
            is_default=1,
            support_priority_level="3",
        ))
        db.add(ExporterPlan(
            title="Premium",
            description="Unlimited stores and listings, sponsored promotions, 1.5% commission.",
            monthly_subscription_fee=150000,
            transaction_limit=-1,
            commission_percent=1.5,
            max_store=-1,
            max_product=-1,
            max_market=-1,
            product_promotion=1,
            max_product_promotion=10,
            currency="NGN",
            is_default=0,
            support_priority_level="1",
        ))


def _seed_settings(db: Session) -> None:
    if db.query(Setting).filter_by(key="commission_account").count() == 0:
        db.add(Setting(key="commission_account", value=json.dumps({
            "bank_name": "GTBank",
            "account_name": "Jaratrade Ltd",
            "account_number": "0000000000",
        })))


def _seed_demo_users(db: Session) -> None:
    """Create one user per role for easy testing."""
    if db.query(User).count() > 0:
        return

    # Admin
    admin = User(
        role=ROLE_ADMIN,
        kind="business",
        email="admin@jaratrade.com",
        password_hash=hash_password("REDACTED-old-default"),
        firstname="Platform",
        lastname="Admin",
        profile_name="admin",
        is_active=True,
        email_verified=True,
        country="Nigeria",
    )
    db.add(admin)

    # Demo exporter (pre-approved so the demo experience is unblocked)
    exporter = User(
        role=ROLE_EXPORTER,
        kind="business",
        email="exporter@jaratrade.com",
        password_hash=hash_password("REDACTED-old-default"),
        firstname="Adaeze",
        lastname="Okafor",
        kyc_status="approved",
        phone="+2348100000001",
        address="6 Ojo Road",
        country="Nigeria",
        profile_name="adaeze-foods",
        is_active=True,
        email_verified=True,
    )
    db.add(exporter)
    db.flush()
    # Seed a banking record so the exporter is payout-ready out of the gate.
    # Picks the first Nigerian bank that has a flutter_code, with a dev-grade
    # 10-digit account number that resolves cleanly against the FLW dev stub.
    sample_bank = db.query(Bank).filter(Bank.flutter_code.isnot(None)).first()
    db.add(BusinessProfile(
        user_id=exporter.id,
        business_name="Adaeze Foods Ltd",
        business_email="hello@adaezefoods.com",
        business_address="Stall 12, Mushin Market",
        business_reg_number="RC123456",
        business_type="food_beverage",
        annual_turnover="1m_5m",
        duration_in_business=4,
        tin="TIN789",
        valid_identification="passport",
        bank_id=sample_bank.id if sample_bank else None,
        account_number="0123456789",
        account_name="Adaeze Foods Ltd",
    ))

    # Demo importer
    importer = User(
        role=ROLE_IMPORTER,
        kind="individual",
        email="importer@jaratrade.com",
        password_hash=hash_password("REDACTED-old-default"),
        firstname="Tunde",
        lastname="Adebayo",
        phone="+447400000001",
        address="42 Brixton Road, London",
        country="United Kingdom",
        city="London",
        profile_name="tunde-imports",
        is_active=True,
        email_verified=True,
    )
    db.add(importer)
    db.flush()

    # Seed a couple of demo products on the demo exporter
    market = db.query(Market).first()
    category = db.query(Category).first()
    if market and category:
        store = Store(exporter_id=exporter.id, market_id=market.id, address="Stall 12", is_default=1)
        db.add(store)
        db.flush()
        for name, price, desc in [
            ("Premium Garri (50kg)", 35000, "Stone-free yellow garri, sun-dried, sealed"),
            ("Suya Spice Mix (1kg)", 8500, "Authentic Northern blend with peanut, ginger, cayenne"),
            ("Dried Stockfish (5kg)", 60000, "Sun-dried, premium grade, suitable for export"),
            ("Plantain Chips (Carton, 24x100g)", 18000, "Crispy salted plantain chips in retail packets"),
        ]:
            db.add(Product(
                exporter_id=exporter.id,
                store_id=store.id,
                category_id=category.id,
                product_name=name,
                description=desc,
                price=price,
                currency="NGN",
                images=json.dumps([
                    "https://res.cloudinary.com/do4nw8sul/image/upload/v1727867535/products/hfnmkkfkompwubsoqxdf.jpg"
                ]),
                min_order_quantity=2,
                max_order_quantity=100,
                properties=json.dumps({"weight": "50kg"}),
                is_featured=1,
                stock_quantity=80,
                low_stock_threshold=15,
                last_inventory_update_at=datetime.now(timezone.utc).replace(tzinfo=None),
                status=1,
            ))
