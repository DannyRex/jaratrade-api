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
    Dispute,
    ExporterPlan,
    ImporterPlan,
    LogisticsCompany,
    LogisticsRate,
    Market,
    Order,
    OrderItem,
    Payment,
    Payout,
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


# ───────────────────────── Orders (admin overview) ─────────────────────────

@router.get("/orders/stats")
def orders_stats(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Headline numbers for the admin orders dashboard.

    Returns counts by status and GMV - the gross merchandise value
    summed from `Order.total` for paid+ orders so we don't count
    abandoned-cart `pending` rows.
    """
    from sqlalchemy import func

    counts = dict(
        db.query(Order.status, func.count(Order.id)).group_by(Order.status).all()
    )
    gmv_row = (
        db.query(func.coalesce(func.sum(Order.total), 0.0))
        .filter(Order.status.in_(["paid", "confirmed", "preparing", "shipped", "delivered"]))
        .first()
    )
    gmv = float(gmv_row[0] or 0.0) if gmv_row else 0.0

    # "Pending payouts" must mean the same thing the /admin/payouts
    # "Eligible" tab shows, otherwise the orders-dashboard card and the
    # payouts screen disagree (card said 1, screen showed none, because a
    # delivered order still inside its 7-day dispute window counted on the
    # card but isn't eligible yet). Reuse the exact eligibility rule:
    # delivered + (past dispute window OR buyer-confirmed) + has a
    # successful payment + no payout dispatched.
    from ..routers.payouts import _is_payout_eligible

    # Failed payouts don't count - those orders still need to be paid out.
    orders_with_payout = {
        row[0]
        for row in db.query(Payout.order_id).filter(Payout.status != "failed").all()
    }
    pending_payouts = 0
    for o in db.query(Order).filter(Order.status == "delivered").all():
        if o.id in orders_with_payout:
            continue
        if not _is_payout_eligible(o):
            continue
        has_payment = (
            db.query(Payment)
            .filter(Payment.order_id == o.id, Payment.status == "successful")
            .first()
        )
        if has_payment:
            pending_payouts += 1

    # "Open disputes" means unresolved work: a freshly-raised dispute ('open')
    # OR one an admin has acknowledged but not yet closed ('in_review'). The
    # status was previously checked against a non-existent 'investigating'
    # value, so acknowledged disputes silently dropped off this card.
    open_disputes = (
        db.query(func.count(Dispute.id))
        .filter(Dispute.status.in_(["open", "in_review"]))
        .scalar()
    ) or 0

    return success({
        "total_orders": sum(counts.values()),
        "by_status": {k: int(v) for k, v in counts.items()},
        "gmv": f"{gmv:.2f}",
        "pending_payouts": int(pending_payouts),
        "open_disputes": int(open_disputes),
    })


@router.get("/orders")
def list_admin_orders(
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Search order #, buyer email, or seller business"),
    p: int = Query(default=0, ge=0),
    len_: int = Query(default=25, ge=1, le=100, alias="len"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Paginated orders list enriched with buyer, seller, payment + payout
    state.

    This replaces the side-effect behaviour of `GET /adm/logistics` when
    filter params were passed - that endpoint goes back to returning the
    logistics-partner list. Frontends should call this for the orders
    overview.
    """
    from sqlalchemy import func, or_, select

    query = db.query(Order)
    if status:
        query = query.filter(Order.status == status)
    if q:
        like = f"%{q.strip()}%"
        # Join in users + business_profiles only when searching, so the
        # default list stays cheap.
        from ..models.user import BusinessProfile
        buyer_match = select(User.id).where(User.email.ilike(like))
        seller_match = select(BusinessProfile.user_id).where(
            BusinessProfile.business_name.ilike(like)
        )
        query = query.filter(
            or_(
                Order.order_number.ilike(like),
                Order.id.ilike(like),
                Order.importer_id.in_(buyer_match),
                Order.exporter_id.in_(seller_match),
            )
        )

    total = query.count()
    orders = (
        query.order_by(desc(Order.time_created))
        .offset(p * len_)
        .limit(len_)
        .all()
    )

    # Bulk-load related rows so we don't N+1 on the response.
    order_ids = [o.id for o in orders]
    user_ids = list({o.importer_id for o in orders} | {o.exporter_id for o in orders if o.exporter_id})
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    item_counts = dict(
        db.query(OrderItem.order_id, func.count(OrderItem.id))
        .filter(OrderItem.order_id.in_(order_ids))
        .group_by(OrderItem.order_id)
        .all()
    ) if order_ids else {}
    payments_by_order: dict[str, str] = {}
    if order_ids:
        for pay in db.query(Payment).filter(Payment.order_id.in_(order_ids)).all():
            # Prefer "successful" over any pending row for a given order.
            cur = payments_by_order.get(pay.order_id)
            if cur != "successful":
                payments_by_order[pay.order_id] = pay.status
    payouts_by_order = {
        po.order_id: po.status for po in (
            db.query(Payout).filter(Payout.order_id.in_(order_ids)).all() if order_ids else []
        )
    }
    # has_dispute drives a warning icon labelled "open dispute" in the grid,
    # so only flag orders with an UNRESOLVED dispute (open / in_review) - a
    # resolved or rejected dispute is closed and shouldn't raise the flag.
    disputed = set()
    if order_ids:
        for (oid,) in (
            db.query(Dispute.order_id)
            .filter(
                Dispute.order_id.in_(order_ids),
                Dispute.status.in_(["open", "in_review"]),
            )
            .all()
        ):
            disputed.add(oid)

    rows = []
    for o in orders:
        buyer = users.get(o.importer_id)
        seller = users.get(o.exporter_id) if o.exporter_id else None
        seller_biz = seller.business if seller else None
        rows.append({
            "id": o.id,
            "order_id": o.order_number,
            "status": o.status,
            "total": f"{float(o.total):.2f}",
            "currency": o.currency,
            "items_count": int(item_counts.get(o.id, 0)),
            "time_created": o.time_created.isoformat(),
            "time_updated": o.time_updated.isoformat(),
            "confirmed_received_at": (
                o.confirmed_received_at.isoformat() if o.confirmed_received_at else None
            ),
            "buyer": {
                "id": buyer.id if buyer else None,
                "name": (buyer.fullname if buyer else None) or (buyer.email if buyer else None),
                "email": buyer.email if buyer else None,
            },
            "seller": {
                "id": seller.id if seller else None,
                "business_name": seller_biz.business_name if seller_biz else None,
                "email": seller.email if seller else None,
            },
            "payment_status": payments_by_order.get(o.id),
            "payout_status": payouts_by_order.get(o.id),  # None | pending | sent | completed | failed
            "has_dispute": o.id in disputed,
        })

    return success({"rows": rows, "total_length": total, "page": p, "len": len_})


@router.get("/orders/{order_id}")
def get_admin_order(
    order_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Full detail for the admin order drawer.

    Includes everything from the list endpoint plus line items, delivery
    info, all payments + all payouts so admin can audit a single order in
    one shot without juggling multiple endpoints.
    """
    import json as _json

    order = db.get(Order, order_id)
    if not order:
        # Allow lookup by order_number as a convenience.
        order = db.query(Order).filter(Order.order_number == order_id).first()
    if not order:
        raise fail("Order not found", code=404)

    buyer = db.get(User, order.importer_id) if order.importer_id else None
    seller = db.get(User, order.exporter_id) if order.exporter_id else None
    seller_biz = seller.business if seller else None

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    payments = db.query(Payment).filter(Payment.order_id == order.id).all()
    payouts = db.query(Payout).filter(Payout.order_id == order.id).all()
    # Most recent dispute - an order can have several over its lifetime.
    dispute = (
        db.query(Dispute)
        .filter(Dispute.order_id == order.id)
        .order_by(desc(Dispute.time_created))
        .first()
    )

    return success({
        "id": order.id,
        "order_id": order.order_number,
        "status": order.status,
        "total": f"{float(order.total):.2f}",
        "platform_fee": f"{float(order.platform_fee):.2f}",
        "logistics_fee": f"{float(order.logistics_fee):.2f}",
        "currency": order.currency,
        "shipping_mode": order.shipping_mode,
        "logistics_id": order.logistics_id,
        "delivery_info": _json.loads(order.delivery_info) if order.delivery_info else {},
        "time_created": order.time_created.isoformat(),
        "time_updated": order.time_updated.isoformat(),
        "confirmed_received_at": (
            order.confirmed_received_at.isoformat() if order.confirmed_received_at else None
        ),
        "buyer": {
            "id": buyer.id if buyer else None,
            "name": (buyer.fullname if buyer else None) or (buyer.email if buyer else None),
            "email": buyer.email if buyer else None,
            "phone": buyer.phone if buyer else None,
        },
        "seller": {
            "id": seller.id if seller else None,
            "business_name": seller_biz.business_name if seller_biz else None,
            "email": seller.email if seller else None,
            "phone": seller.phone if seller else None,
        },
        "items": [{
            "id": it.id,
            "product_id": it.product_id,
            "product_name": it.product_name,
            "quantity": it.quantity,
            "unit_price": f"{float(it.unit_price):.2f}",
            "subtotal": f"{float(it.subtotal):.2f}",
        } for it in items],
        "payments": [{
            "id": p.id,
            "tx_ref": p.tx_ref,
            "amount": f"{float(p.amount):.2f}",
            "currency": p.currency,
            "status": p.status,
            "provider": p.provider,
            "time_created": p.time_created.isoformat(),
        } for p in payments],
        "payouts": [{
            "id": po.id,
            "reference": po.reference,
            "amount": f"{float(po.amount):.2f}",
            "currency": po.currency,
            "status": po.status,
            "failure_reason": po.failure_reason,
            "time_created": po.time_created.isoformat(),
        } for po in payouts],
        "dispute": {
            "id": dispute.id,
            "status": dispute.status,
            "reason": dispute.reason,
        } if dispute else None,
    })
