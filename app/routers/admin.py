"""Admin-scoped endpoints - markets, categories, banks, logistics, plans."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import (
    Bank,
    Category,
    ExporterPlan,
    ImporterPlan,
    LogisticsCompany,
    LogisticsRate,
    Market,
    Order,
    User,
)
from ..routers.public import (
    _serialize_bank,
    _serialize_category,
    _serialize_exporter_plan,
    _serialize_importer_plan,
    _serialize_logistics,
    _serialize_market,
)

router = APIRouter(prefix="/adm", tags=["admin"])


def _paged(rows, page: int = 0, length: int = 50):
    return success({"rows": rows, "total_length": len(rows), "page": page, "len": length})


# ───────────────────────── Markets ─────────────────────────

@router.get("/market")
def list_markets(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = [_serialize_market(m) for m in db.query(Market).order_by(Market.name).all()]
    return _paged(rows)


@router.put("/market")
def create_market(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    location: str = Form(...),
    lga: Optional[str] = Form(default=None),
    city: str = Form(...),
    state: Optional[str] = Form(default=None),
    country: str = Form(default="Nigeria"),
):
    m = Market(name=name, location=location, lga=lga, city=city, state=state, country=country)
    db.add(m)
    db.commit()
    return success(_serialize_market(m))


@router.post("/market/{market_id}")
def update_market(
    market_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: Optional[str] = Form(default=None),
    location: Optional[str] = Form(default=None),
    lga: Optional[str] = Form(default=None),
    city: Optional[str] = Form(default=None),
    state: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
):
    m = db.get(Market, market_id)
    if not m:
        raise fail("Market not found", code=404)
    for k, v in {"name": name, "location": location, "lga": lga, "city": city, "state": state, "country": country}.items():
        if v is not None:
            setattr(m, k, v)
    db.commit()
    return success(_serialize_market(m))


@router.delete("/market/{market_id}")
def delete_market(market_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    m = db.get(Market, market_id)
    if not m:
        raise fail("Market not found", code=404)
    db.delete(m)
    db.commit()
    return success({"deleted": True})


# ───────────────────────── Categories ─────────────────────────

@router.get("/category")
def list_categories(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = [_serialize_category(c) for c in db.query(Category).order_by(Category.name).all()]
    return _paged(rows)


@router.put("/category")
def create_category(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: Optional[str] = Form(default=None),
    parent_category: Optional[str] = Form(default=None),
    is_featured: Optional[int] = Form(default=0),
):
    if db.query(Category).filter(Category.name == name).first():
        raise fail("A category with that name already exists", code=409)
    c = Category(name=name, description=description, parent_category=parent_category, is_featured=is_featured or 0)
    db.add(c)
    db.commit()
    return success(_serialize_category(c))


@router.post("/category/{category_id}")
def update_or_delete_category(
    category_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    is_featured: Optional[int] = Form(default=None),
    delete: Optional[int] = Form(default=None),
):
    c = db.get(Category, category_id)
    if not c:
        raise fail("Category not found", code=404)
    if delete:
        db.delete(c)
        db.commit()
        return success({"deleted": True})
    for k, v in {"name": name, "description": description, "is_featured": is_featured}.items():
        if v is not None:
            setattr(c, k, v)
    db.commit()
    return success(_serialize_category(c))


# ───────────────────────── Banks ─────────────────────────

@router.get("/bank")
def list_banks(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = [_serialize_bank(b) for b in db.query(Bank).order_by(Bank.name).all()]
    return _paged(rows)


@router.put("/bank")
def add_bank(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: Optional[str] = Form(default=None),
    country: str = Form(default="Nigeria"),
    paystack_code: Optional[str] = Form(default=None),
    flutter_code: Optional[str] = Form(default=None),
):
    b = Bank(name=name, description=description, country=country, paystack_code=paystack_code, flutter_code=flutter_code)
    db.add(b)
    db.commit()
    return success(_serialize_bank(b))


# ───────────────────────── Logistics ─────────────────────────

@router.get("/logistics")
def list_logistics(
    exporter_id: Optional[str] = Query(default=None),
    importer_id: Optional[str] = Query(default=None),
    order_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if any([exporter_id, importer_id, order_id, q]):
        # Order-search mode (legacy "View Orders" endpoint behaviour)
        query = db.query(Order)
        if exporter_id:
            query = query.filter(Order.exporter_id == exporter_id)
        if importer_id:
            query = query.filter(Order.importer_id == importer_id)
        if order_id:
            query = query.filter((Order.id == order_id) | (Order.order_number == order_id))
        orders = query.order_by(desc(Order.time_created)).limit(50).all()
        rows = [{
            "id": o.id,
            "order_id": o.order_number,
            "exporter_id": o.exporter_id,
            "importer_id": o.importer_id,
            "logistics_id": o.logistics_id,
            "status": o.status,
            "total": f"{float(o.total):.2f}",
            "currency": o.currency,
            "time_created": o.time_created.isoformat(),
        } for o in orders]
        return success({"rows": rows, "total_length": len(rows), "page": 0, "len": len(rows)})

    rows = [_serialize_logistics(l) for l in db.query(LogisticsCompany).order_by(LogisticsCompany.name).all()]
    return _paged(rows)


@router.put("/logistics")
def create_logistics(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: Optional[str] = Form(default=None),
    email: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
):
    l = LogisticsCompany(name=name, description=description, email=email, phone=phone)
    db.add(l)
    db.commit()
    return success(_serialize_logistics(l))


@router.post("/logistics/{logistics_id}")
def update_logistics(
    logistics_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    email: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
):
    l = db.get(LogisticsCompany, logistics_id)
    if not l:
        raise fail("Partner not found", code=404)
    for k, v in {"name": name, "description": description, "email": email, "phone": phone}.items():
        if v is not None:
            setattr(l, k, v)
    db.commit()
    return success(_serialize_logistics(l))


@router.delete("/logistics/{logistics_id}")
def delete_logistics(logistics_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    l = db.get(LogisticsCompany, logistics_id)
    if not l:
        raise fail("Partner not found", code=404)
    db.delete(l)
    db.commit()
    return success({"deleted": True})


@router.patch("/logistics/{order_id}")
def update_delivery_status(
    order_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    status: str = Form(...),
):
    order = db.get(Order, order_id)
    if not order:
        raise fail("Order not found", code=404)
    order.status = status
    db.commit()
    return success({"status": status})


@router.put("/logistics_rate")
def add_logistics_rate(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    logistics_id: str = Form(...),
    origin_country: str = Form(default="Nigeria"),
    destination_country: str = Form(default="United Kingdom"),
    base_rate: float = Form(default=0),
    per_kg_rate: float = Form(default=0),
    currency: str = Form(default="NGN"),
):
    rate = LogisticsRate(
        logistics_id=logistics_id,
        origin_country=origin_country,
        destination_country=destination_country,
        base_rate=base_rate,
        per_kg_rate=per_kg_rate,
        currency=currency,
    )
    db.add(rate)
    db.commit()
    return success({"id": rate.id})


# ───────────────────────── Plans ─────────────────────────

@router.put("/importer_plan")
def create_importer_plan(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    monthly_subscription_fee: float = Form(default=0),
    annual_subscription_fee: float = Form(default=0),
    transaction_limit: float = Form(default=-1),
    commission_value: float = Form(default=-1),
    commission_percent: float = Form(default=2),
    product_limit: int = Form(default=-1),
    currency: str = Form(default="GBP"),
    is_default: int = Form(default=0),
):
    if is_default:
        db.query(ImporterPlan).update({"is_default": 0})
    p = ImporterPlan(
        title=title, description=description,
        monthly_subscription_fee=monthly_subscription_fee, annual_subscription_fee=annual_subscription_fee,
        transaction_limit=transaction_limit, commission_value=commission_value, commission_percent=commission_percent,
        product_limit=product_limit, currency=currency, is_default=is_default,
    )
    db.add(p)
    db.commit()
    return success(_serialize_importer_plan(p))


@router.put("/exporter_plan")
def create_exporter_plan(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    monthly_subscription_fee: float = Form(default=0),
    annual_subscription_fee: float = Form(default=0),
    transaction_limit: float = Form(default=-1),
    commission_value: float = Form(default=-1),
    commission_percent: float = Form(default=2),
    max_market: int = Form(default=-1),
    max_store: int = Form(default=-1),
    max_product: int = Form(default=-1),
    product_promotion: int = Form(default=0),
    max_product_promotion: int = Form(default=0),
    currency: str = Form(default="NGN"),
    is_default: int = Form(default=0),
):
    if is_default:
        db.query(ExporterPlan).update({"is_default": 0})
    p = ExporterPlan(
        title=title, description=description,
        monthly_subscription_fee=monthly_subscription_fee, annual_subscription_fee=annual_subscription_fee,
        transaction_limit=transaction_limit, commission_value=commission_value, commission_percent=commission_percent,
        max_market=max_market, max_store=max_store, max_product=max_product,
        product_promotion=product_promotion, max_product_promotion=max_product_promotion,
        currency=currency, is_default=is_default,
    )
    db.add(p)
    db.commit()
    return success(_serialize_exporter_plan(p))


@router.get("/exporter_subscription")
def get_exporter_subscriptions(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Lists exporter accounts and their current plan id."""
    rows = []
    for u in db.query(User).filter(User.role == "exporter").all():
        biz = u.business
        rows.append({
            "id": u.id,
            "email": u.email,
            "business_name": biz.business_name if biz else None,
            "plan_id": u.plan_id,
            "is_active": u.is_active,
            "email_verified": u.email_verified,
        })
    return success({"rows": rows, "total_length": len(rows), "page": 0, "len": len(rows)})
