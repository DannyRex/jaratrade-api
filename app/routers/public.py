"""Public (unauthenticated) routes - reference data, catalog, support."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from ..constants import ROLE_EXPORTER
from ..database import get_db
from ..envelope import fail, success
from ..models import (
    Bank,
    Category,
    ExporterPlan,
    ImporterPlan,
    LogisticsCompany,
    Market,
    PasswordResetToken,
    Product,
    Store,
    SupportTicket,
    User,
)
from ..security import hash_password, secure_token

router = APIRouter(prefix="/public", tags=["public"])


# ───────────────────────── Reference data ─────────────────────────

def _paged_rows(rows, total: int, page: int = 0, length: int = 50):
    return success({"rows": rows, "total_length": total, "page": page, "len": length})


@router.get("/data/category")
def list_categories(db: Session = Depends(get_db)):
    items = db.query(Category).filter(Category.status == 1).order_by(Category.name).all()
    rows = [_serialize_category(c) for c in items]
    return _paged_rows(rows, len(rows))


@router.get("/data/market")
def list_markets(db: Session = Depends(get_db)):
    items = db.query(Market).filter(Market.status == 1).order_by(Market.name).all()
    rows = [_serialize_market(m) for m in items]
    return _paged_rows(rows, len(rows))


@router.get("/data/bank")
def list_banks(db: Session = Depends(get_db)):
    items = db.query(Bank).filter(Bank.status == 1).order_by(Bank.name).all()
    rows = [_serialize_bank(b) for b in items]
    return _paged_rows(rows, len(rows))


@router.get("/data/logistics")
def list_logistics(db: Session = Depends(get_db)):
    items = db.query(LogisticsCompany).filter(LogisticsCompany.status == 1).order_by(LogisticsCompany.name).all()
    rows = [_serialize_logistics(l) for l in items]
    return _paged_rows(rows, len(rows))


@router.get("/data/importer_plan")
def list_importer_plans(db: Session = Depends(get_db)):
    items = db.query(ImporterPlan).filter(ImporterPlan.status == 1).order_by(ImporterPlan.monthly_subscription_fee).all()
    rows = [_serialize_importer_plan(p) for p in items]
    return _paged_rows(rows, len(rows))


@router.get("/data/exporter_plan")
def list_exporter_plans(db: Session = Depends(get_db)):
    items = db.query(ExporterPlan).filter(ExporterPlan.status == 1).order_by(ExporterPlan.monthly_subscription_fee).all()
    rows = [_serialize_exporter_plan(p) for p in items]
    return _paged_rows(rows, len(rows))


# ───────────────────────── Marketplace ─────────────────────────

@router.get("")
def home(db: Session = Depends(get_db)):
    """Aggregate home page payload - top exporters, top products, top categories."""
    exporters = (
        db.query(User)
        .filter(User.role == ROLE_EXPORTER, User.is_active.is_(True))
        .order_by(desc(User.product_delivered))
        .limit(10)
        .all()
    )
    top_exporters = [_serialize_top_exporter(u) for u in exporters]

    products = (
        db.query(Product)
        .join(User, Product.exporter_id == User.id)
        .filter(Product.status == 1)
        .order_by(desc(Product.is_featured), desc(Product.views), desc(Product.time_created))
        .limit(12)
        .all()
    )
    top_products = [_serialize_product_summary(p, db) for p in products]

    cats = (
        db.query(Category, func.count(Product.id).label("cat_count"))
        .outerjoin(Product, Product.category_id == Category.id)
        .filter(Category.status == 1)
        .group_by(Category.id)
        .order_by(desc("cat_count"))
        .limit(8)
        .all()
    )
    top_categories = [_serialize_category(c, count) for c, count in cats]

    return success({
        "top_exporter": top_exporters,
        "top_products": top_products,
        "top_categories": top_categories,
    })


@router.get("/products")
def list_products(
    db: Session = Depends(get_db),
    category: Optional[str] = Query(default=None),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=15, ge=1, le=100, alias="len"),
    sort_by: Optional[str] = Query(default=None),
    exporter: Optional[str] = Query(default=None),
    store: Optional[str] = Query(default=None),
):
    q = db.query(Product).filter(Product.status == 1)
    if category:
        q = q.join(Category).filter(or_(Category.id == category, Category.name == category))
    if exporter:
        q = q.filter(Product.exporter_id == exporter)
    if store:
        q = q.filter(Product.store_id == store)
    total = q.count()
    # Always surface sponsored (promote=1) listings before everything else
    if sort_by == "price_asc":
        q = q.order_by(desc(Product.promote), Product.price.asc())
    elif sort_by == "price_desc":
        q = q.order_by(desc(Product.promote), Product.price.desc())
    elif sort_by == "popular":
        q = q.order_by(desc(Product.promote), desc(Product.views))
    else:
        q = q.order_by(desc(Product.promote), desc(Product.time_created))
    items = q.offset(p * len_).limit(len_).all()
    rows = [_serialize_product_summary(prod, db) for prod in items]
    return success({"data": rows, "meta": {"paging": {"total": total, "page": p + 1, "len": len_}}})


@router.get("/products/{product_id}")
def fetch_product(product_id: str, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod or prod.status != 1:
        raise fail("Product not found", code=404)
    prod.views = (prod.views or 0) + 1
    db.commit()
    return success(_serialize_product_detail(prod, db))


@router.post("/{product_id}")
def save_product_view(product_id: str, db: Session = Depends(get_db)):
    """Legacy endpoint to bump product view count."""
    prod = db.get(Product, product_id)
    if prod:
        prod.views = (prod.views or 0) + 1
        db.commit()
    return success({"recorded": True})


# ───────────────────────── Public reviews ─────────────────────────

@router.get("/reviews/exporter/{exporter_id}")
def list_exporter_reviews(
    exporter_id: str,
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=20, ge=1, le=100, alias="len"),
    db: Session = Depends(get_db),
):
    """Public reviews + rating distribution for an exporter."""
    from ..models import Review

    q = db.query(Review).filter(Review.exporter_id == exporter_id)
    total = q.count()
    rows = q.order_by(desc(Review.time_created)).offset(p * len_).limit(len_).all()

    # Rating distribution (1-5)
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in db.query(Review).filter(Review.exporter_id == exporter_id).all():
        distribution[str(r.rating)] = distribution.get(str(r.rating), 0) + 1
    avg = (sum(int(k) * v for k, v in distribution.items()) / total) if total else 0.0

    return success({
        "rows": [{
            "id": r.id,
            "rating": r.rating,
            "comment": r.comment,
            "order_id": r.order_id,
            "time_created": r.time_created.isoformat(),
        } for r in rows],
        "total_length": total,
        "page": p,
        "len": len_,
        "average_rating": round(avg, 2),
        "distribution": distribution,
    })


# ───────────────────────── Support ─────────────────────────

@router.post("/support")
def submit_support(
    db: Session = Depends(get_db),
    firstname: str = Form(...),
    lastname: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
):
    db.add(SupportTicket(firstname=firstname, lastname=lastname, phone=phone, email=email, subject=subject, message=message))
    db.commit()
    return success({"submitted": True}, message="Thanks - we'll get back to you within 48 hours.")


# ───────────────────────── Password reset ─────────────────────────

@router.post("/auth/password_reset")
def request_password_reset(
    db: Session = Depends(get_db),
    email: str = Form(...),
    user_type: str = Form(...),
):
    if user_type not in ("importer", "exporter"):
        raise fail("Invalid user_type")

    user = db.query(User).filter(User.email == email, User.role == user_type).first()
    if user:
        token = PasswordResetToken(
            user_id=user.id,
            code=secure_token(32),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.add(token)
        db.commit()
        # Email sent here in production - print in dev for visibility.
        from ..services.email import password_reset_email, send_email
        from ..config import get_settings
        s = get_settings()
        send_email(
            to=user.email,
            subject="Reset your Jaratrade password",
            html=password_reset_email(user.firstname, f"{s.site_url}/auth/reset-password?code={token.code}&user_type={user_type}"),
        )
    # Always return success - don't leak account existence
    return success({"sent": True}, message="If an account exists, we've sent a reset link.")


@router.get("/auth/password_reset")
def verify_reset_code(
    code: str = Query(...),
    user_type: str = Query(...),
    db: Session = Depends(get_db),
):
    token = db.query(PasswordResetToken).filter(PasswordResetToken.code == code).first()
    if not token or token.used or token.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise fail("Reset link is invalid or expired", code=400)
    return success({"valid": True})


@router.post("/auth/change_password")
def change_password(
    db: Session = Depends(get_db),
    code: str = Form(...),
    new_password: str = Form(...),
    user_type: str = Form(...),
):
    if len(new_password) < 8:
        raise fail("Password must be at least 8 characters")
    token = db.query(PasswordResetToken).filter(PasswordResetToken.code == code).first()
    if not token or token.used or token.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise fail("Reset link is invalid or expired", code=400)
    user = db.get(User, token.user_id)
    if not user:
        raise fail("Account not found", code=404)
    user.password_hash = hash_password(new_password)
    token.used = True
    db.commit()
    return success({"updated": True})


# ───────────────────────── Serializers ─────────────────────────

def _serialize_top_exporter(u: User) -> dict:
    biz = u.business
    return {
        "id": u.id,
        "profile_name": u.profile_name or "",
        "fullname": u.fullname or "",
        "email": u.email,
        "phone": u.phone or "",
        "address": u.address or "",
        "business_name": biz.business_name if biz else "",
        "passport": u.passport,
        "exporter_country": u.country,
        "business_country": biz.business_country if biz else None,
        "business_email": biz.business_email if biz else "",
        "business_address": biz.business_address if biz else "",
        "business_reg_number": biz.business_reg_number if biz else "",
        "order_count": u.product_delivered or 0,
    }


def _serialize_product_summary(p: Product, db: Session) -> dict:
    exporter = db.get(User, p.exporter_id)
    biz = exporter.business if exporter else None
    cat = db.get(Category, p.category_id)
    store = db.get(Store, p.store_id)
    market = db.get(Market, store.market_id) if store else None
    return {
        "id": p.id,
        "exporter_id": p.exporter_id,
        "exporter_name": exporter.fullname if exporter else "",
        "business_name": biz.business_name if biz else "",
        "product_name": p.product_name,
        "description": p.description or "",
        "category": cat.name if cat else "",
        "store": f"{store.address}" if store else "",
        "price": f"{float(p.price):.2f}",
        "currency": p.currency,
        "images": p.images or "[]",
        "properties": p.properties or "{}",
        "market_name": market.name if market else "",
        "location": market.location if market else "",
        "is_featured": p.is_featured,
        "promote": p.promote,
        "status": p.status,
    }


def _serialize_product_detail(p: Product, db: Session) -> dict:
    cat = db.get(Category, p.category_id)
    store = db.get(Store, p.store_id)
    market = db.get(Market, store.market_id) if store else None
    return {
        "id": p.id,
        "product_name": p.product_name,
        "description": p.description or "",
        "category_id": p.category_id,
        "store_id": p.store_id,
        "price": f"{float(p.price):.2f}",
        "currency": p.currency,
        "images": p.images or "[]",
        "short_video_link": p.short_video_link or "",
        "min_order_quantity": p.min_order_quantity,
        "max_order_quantity": p.max_order_quantity,
        "properties": p.properties or "{}",
        "views": p.views,
        "has_tax": p.has_tax,
        "is_featured": p.is_featured,
        "status": p.status,
        "time_created": p.time_created.isoformat() if p.time_created else None,
        "time_updated": p.time_updated.isoformat() if p.time_updated else None,
        "exporter_id": p.exporter_id,
        "promote": p.promote,
        "name": cat.name if cat else "",  # category name (legacy field)
        "store": store.address if store else "",
        "view_counts": p.views,
        "market_name": market.name if market else "",
        "market_location": market.location if market else "",
        "market_id": store.market_id if store else "",
    }


def _serialize_category(c: Category, cat_count: Optional[int] = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "views": c.views or 0,
        "parent_category": c.parent_category,
        "image": c.image,
        "is_featured": c.is_featured,
        "cat_count": cat_count if cat_count is not None else 0,
        "time_created": c.time_created.isoformat() if c.time_created else None,
        "time_updated": c.time_updated.isoformat() if c.time_updated else None,
        "status": c.status,
    }


def _serialize_market(m: Market) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "location": m.location,
        "lga": m.lga or "",
        "city": m.city,
        "state": m.state or "",
        "country": m.country,
        "status": m.status,
        "time_created": m.time_created.isoformat() if m.time_created else None,
        "time_updated": m.time_updated.isoformat() if m.time_updated else None,
    }


def _serialize_bank(b: Bank) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "description": b.description or "",
        "country": b.country,
        "paystack_code": b.paystack_code,
        "flutter_code": b.flutter_code,
        "status": b.status,
        "time_created": b.time_created.isoformat() if b.time_created else None,
        "time_updated": b.time_updated.isoformat() if b.time_updated else None,
    }


def _serialize_logistics(l: LogisticsCompany) -> dict:
    return {
        "id": l.id,
        "name": l.name,
        "description": l.description or "",
        "email": l.email or "",
        "phone": l.phone or "",
        "status": l.status,
    }


def _serialize_importer_plan(p: ImporterPlan) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "description": p.description or "",
        "monthly_subscription_fee": f"{float(p.monthly_subscription_fee):.2f}",
        "annual_subscription_fee": f"{float(p.annual_subscription_fee):.2f}",
        "transaction_limit": f"{float(p.transaction_limit):.2f}",
        "commission_value": f"{float(p.commission_value):.2f}",
        "commission_percent": f"{float(p.commission_percent):.2f}",
        "product_limit": p.product_limit,
        "currency": p.currency,
        "is_default": p.is_default,
        "time_created": p.time_created.isoformat() if p.time_created else None,
        "time_updated": p.time_updated.isoformat() if p.time_updated else None,
        "status": p.status,
    }


def _serialize_exporter_plan(p: ExporterPlan) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "description": p.description or "",
        "monthly_subscription_fee": f"{float(p.monthly_subscription_fee):.2f}",
        "annual_subscription_fee": f"{float(p.annual_subscription_fee):.2f}",
        "transaction_limit": f"{float(p.transaction_limit):.2f}",
        "commission_value": f"{float(p.commission_value):.2f}",
        "commission_percent": f"{float(p.commission_percent):.2f}",
        "product_promotion": p.product_promotion,
        "max_product_promotion": p.max_product_promotion,
        "max_market": p.max_market,
        "max_store": p.max_store,
        "max_store_per_market": p.max_store_per_market,
        "max_product_per_store": p.max_product_per_store,
        "max_product": p.max_product,
        "support_priority_level": p.support_priority_level,
        "is_default": p.is_default,
        "currency": p.currency,
        "status": p.status,
        "time_created": p.time_created.isoformat() if p.time_created else None,
        "time_updated": p.time_updated.isoformat() if p.time_updated else None,
    }
