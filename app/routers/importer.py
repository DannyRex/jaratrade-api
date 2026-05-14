"""Importer-scoped endpoints - cart, orders, payments, profile, shipping, favourites."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import require_importer
from ..envelope import fail, success
from ..models import (
    Cart,
    CartItem,
    FavouriteProduct,
    ImporterPlan,
    Order,
    OrderItem,
    Payment,
    Product,
    Review,
    ShippingAddress,
    User,
)
from ..models.user import BusinessProfile
from ..rate_limit import limiter
from ..routers.public import _serialize_product_summary
from ..services.email import (
    send_template,
    t_order_placed_buyer,
    t_order_received_seller,
    t_order_status_update,
    t_payment_invoice,
    t_review_received,
    t_transaction_limit_warning,
)
from ..services.flutterwave import build_inline_config, verify_payment
from ..services.fx import convert as fx_convert

router = APIRouter(prefix="/imp", tags=["importer"])
settings = get_settings()


# ───────────────────────── Profile ─────────────────────────

@router.get("/profile")
def get_profile(
    fav_prod: Optional[int] = Query(default=None),
    reviews: Optional[int] = Query(default=None),
    p: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    if fav_prod:
        favs = (
            db.query(Product)
            .join(FavouriteProduct, FavouriteProduct.product_id == Product.id)
            .filter(FavouriteProduct.user_id == user.id, Product.status == 1)
            .order_by(desc(FavouriteProduct.time_created))
            .offset((p - 1) * size)
            .limit(size)
            .all()
        )
        rows = [_serialize_product_summary(prod, db) for prod in favs]
        return success({"rows": rows, "total_length": len(rows), "page": p, "len": size})

    if reviews:
        rs = db.query(Review).filter(Review.importer_id == user.id).order_by(desc(Review.time_created)).all()
        return success({"rows": [{
            "id": r.id,
            "exporter_id": r.exporter_id,
            "order_id": r.order_id,
            "rating": r.rating,
            "comment": r.comment,
            "time_created": r.time_created.isoformat(),
        } for r in rs], "total_length": len(rs), "page": 1, "len": len(rs)})

    biz = user.business
    return success({
        "id": user.id,
        "firstname": user.firstname,
        "middlename": user.middlename,
        "lastname": user.lastname,
        "phone": user.phone,
        "address": user.address,
        "city": user.city,
        "state": user.state,
        "country": user.country,
        "dob": user.dob,
        "profile_name": user.profile_name,
        "fav_product": [],
        "product_delivered": user.product_delivered,
        "review_count": user.review_count,
        "status": user.status,
        "business": _biz_dict(biz),
    })


def _biz_dict(biz: Optional[BusinessProfile]) -> dict:
    if not biz:
        return {}
    return {
        "business_name": biz.business_name,
        "business_email": biz.business_email,
        "business_address": biz.business_address,
        "business_reg_number": biz.business_reg_number,
        "business_type": biz.business_type,
        "annual_turnover": biz.annual_turnover,
        "duration_in_business": biz.duration_in_business,
        "tin": biz.tin,
        "valid_identification": biz.valid_identification,
        "documents": biz.documents,
    }


@router.post("/profile")
def update_profile(
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    firstname: Optional[str] = Form(default=None),
    lastname: Optional[str] = Form(default=None),
    middlename: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
    address: Optional[str] = Form(default=None),
    city: Optional[str] = Form(default=None),
    state: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
    profile_name: Optional[str] = Form(default=None),
):
    for k, v in {
        "firstname": firstname, "lastname": lastname, "middlename": middlename,
        "phone": phone, "address": address, "city": city,
        "state": state, "country": country, "profile_name": profile_name,
    }.items():
        if v is not None:
            setattr(user, k, v)
    db.commit()
    return success({"updated": True})


# ───────────────────────── Cart ─────────────────────────

def _ensure_cart(db: Session, importer_id: str) -> Cart:
    cart = db.query(Cart).filter(Cart.importer_id == importer_id, Cart.status == "active").first()
    if not cart:
        cart = Cart(importer_id=importer_id, status="active")
        db.add(cart)
        db.commit()
    return cart


@router.post("/cart")
def add_to_cart(
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    product_id: str = Form(...),
    quantity: int = Form(default=1),
    unit: str = Form(default="cartons"),
):
    prod = db.get(Product, product_id)
    if not prod or prod.status != 1:
        raise fail("Product not found", code=404)
    if quantity < (prod.min_order_quantity or 1):
        raise fail(f"Minimum order quantity is {prod.min_order_quantity}")

    cart = _ensure_cart(db, user.id)
    existing = db.query(CartItem).filter(CartItem.cart_id == cart.id, CartItem.product_id == product_id).first()
    if existing:
        existing.quantity += quantity
        existing.subtotal = float(existing.unit_price) * existing.quantity
    else:
        db.add(CartItem(
            cart_id=cart.id,
            product_id=product_id,
            quantity=quantity,
            unit=unit,
            unit_price=prod.price,
            subtotal=float(prod.price) * quantity,
        ))
    db.commit()
    return success({"cart_id": cart.id})


class CartSyncItem(BaseModel):
    product_id: str
    quantity: int = 1
    unit: str = "cartons"


class CartSyncIn(BaseModel):
    items: List[CartSyncItem]
    replace: bool = True  # if True, server cart is replaced with these items


@router.post("/cart/sync")
def sync_cart(
    payload: CartSyncIn,
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    """Bulk-sync the local cart -> server cart.

    Frontend calls this when the user signs in (so a guest cart survives
    login) and after add/remove operations. Returns the canonical server cart.
    """
    cart = _ensure_cart(db, user.id)
    if payload.replace:
        for item in list(cart.items):
            db.delete(item)
        db.flush()

    for item in payload.items:
        if item.quantity <= 0:
            continue
        prod = db.get(Product, item.product_id)
        if not prod or prod.status != 1:
            continue  # skip silently - the product may have been delisted

        existing = db.query(CartItem).filter(
            CartItem.cart_id == cart.id, CartItem.product_id == item.product_id
        ).first()
        if existing and not payload.replace:
            existing.quantity += item.quantity
            existing.subtotal = float(existing.unit_price) * existing.quantity
        elif not existing:
            db.add(CartItem(
                cart_id=cart.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=prod.price,
                subtotal=float(prod.price) * item.quantity,
            ))
    db.commit()
    db.refresh(cart)
    return success(_serialize_cart(cart, db))


@router.get("/cart")
def list_carts(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    carts = db.query(Cart).filter(Cart.importer_id == user.id).all()
    return success({"data": [_serialize_cart(c, db) for c in carts], "meta": {"paging": {"total": len(carts), "page": 1, "len": len(carts)}}})


@router.get("/cart/{cart_id}")
def view_cart(cart_id: str, user: User = Depends(require_importer), db: Session = Depends(get_db)):
    cart = db.get(Cart, cart_id)
    if not cart or cart.importer_id != user.id:
        raise fail("Cart not found", code=404)
    return success(_serialize_cart(cart, db))


@router.delete("/cart/{cart_id}")
def remove_or_clear(
    cart_id: str,
    product_id: Optional[str] = Query(default=None),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    cart = db.get(Cart, cart_id)
    if not cart or cart.importer_id != user.id:
        raise fail("Cart not found", code=404)
    if product_id:
        item = db.query(CartItem).filter(CartItem.cart_id == cart_id, CartItem.product_id == product_id).first()
        if item:
            db.delete(item)
            db.commit()
        return success({"removed": True})
    # Clear whole cart
    for item in list(cart.items):
        db.delete(item)
    db.commit()
    return success({"cleared": True})


def _serialize_cart(cart: Cart, db: Session) -> dict:
    items = []
    for item in cart.items:
        prod = db.get(Product, item.product_id)
        items.append({
            "id": item.id,
            "product_id": item.product_id,
            "name": prod.product_name if prod else "Unknown",
            "category": "",
            "price": f"{float(item.unit_price):.2f}",
            "quantity": item.quantity,
            "unit": item.unit,
            "subtotal": f"{float(item.subtotal):.2f}",
        })
    return {
        "id": cart.id,
        "status": cart.status,
        "items": items,
        "total": f"{sum(float(i.subtotal) for i in cart.items):.2f}",
        "time_created": cart.time_created.isoformat(),
    }


# ───────────────────────── Orders ─────────────────────────

@router.post("/order")
def create_order(
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    cart_id: str = Form(...),
    logistic_id: Optional[str] = Form(default=None),
    delivery_info: str = Form(...),  # JSON string
):
    cart = db.get(Cart, cart_id)
    if not cart or cart.importer_id != user.id:
        raise fail("Cart not found", code=404)
    if not cart.items:
        raise fail("Cart is empty")

    try:
        delivery = json.loads(delivery_info)
    except (TypeError, ValueError):
        delivery = {"raw": delivery_info}

    subtotal = sum(float(i.subtotal) for i in cart.items)
    platform_fee = round(subtotal * 0.02, 2)
    logistics_fee = round(subtotal * 0.05, 2) if logistic_id else 0.0
    total = subtotal + platform_fee + logistics_fee

    # All items in this MVP belong to one exporter; pick the first
    exporter_id = None
    if cart.items:
        first_prod = db.get(Product, cart.items[0].product_id)
        exporter_id = first_prod.exporter_id if first_prod else None

    order_number = "JARA" + secrets.token_urlsafe(6)[:10].upper()
    order = Order(
        order_number=order_number,
        cart_id=cart.id,
        importer_id=user.id,
        exporter_id=exporter_id,
        total=total,
        platform_fee=platform_fee,
        logistics_fee=logistics_fee,
        currency="NGN",
        status="pending",
        shipping_mode="logistics" if logistic_id else "self",
        logistics_id=logistic_id,
        delivery_info=json.dumps(delivery),
    )
    db.add(order)
    db.flush()

    for item in cart.items:
        prod = db.get(Product, item.product_id)
        db.add(OrderItem(
            order_id=order.id,
            product_id=item.product_id,
            product_name=prod.product_name if prod else "Unknown",
            quantity=item.quantity,
            unit_price=item.unit_price,
            subtotal=item.subtotal,
        ))

    cart.status = "ordered"
    db.commit()

    # Notify both parties
    order_link = f"{settings.site_url}/importer/orders/{order.id}"
    subject, html = t_order_placed_buyer(user.firstname or "there", order.order_number, f"{total:.2f}", order_link)
    send_template(
        db, template="order_placed_buyer", to=user.email, subject=subject, html=html,
        user_id=user.id, dedupe_key=f"order_placed_buyer:{order.id}",
    )
    if exporter_id:
        exporter = db.get(User, exporter_id)
        if exporter:
            seller_link = f"{settings.site_url}/exporter/orders/{order.id}"
            subject, html = t_order_received_seller(
                exporter.firstname or "there",
                order.order_number,
                user.fullname or user.email,
                f"{total:.2f}",
                seller_link,
            )
            send_template(
                db, template="order_received_seller", to=exporter.email, subject=subject, html=html,
                user_id=exporter.id, dedupe_key=f"order_received_seller:{order.id}",
            )

    return success({"order_id": order.id, "order_number": order.order_number, "total": f"{total:.2f}"})


@router.get("/order")
def list_orders(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.importer_id == user.id).order_by(desc(Order.time_created)).all()
    rows = [_serialize_order(o) for o in orders]
    return success({"data": rows, "meta": {"paging": {"total": len(rows), "page": 1, "len": len(rows)}}})


@router.get("/order/{order_id}")
def get_order(order_id: str, user: User = Depends(require_importer), db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order or order.importer_id != user.id:
        raise fail("Order not found", code=404)
    return success(_serialize_order(order, include_items=True))


@router.delete("/order/{order_id}")
def cancel_order(order_id: str, user: User = Depends(require_importer), db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order or order.importer_id != user.id:
        raise fail("Order not found", code=404)
    if order.status not in ("pending", "paid"):
        raise fail("Order can no longer be cancelled")
    order.status = "cancelled"
    db.commit()
    return success({"cancelled": True})


def _serialize_order(o: Order, *, include_items: bool = False) -> dict:
    out = {
        "id": o.id,
        "order_id": o.order_number,
        "importer_id": o.importer_id,
        "exporter_id": o.exporter_id,
        "total": f"{float(o.total):.2f}",
        "platform_fee": f"{float(o.platform_fee):.2f}",
        "logistics_fee": f"{float(o.logistics_fee):.2f}",
        "currency": o.currency,
        "status": o.status,
        "shipping_method": o.shipping_mode,
        "logistics_id": o.logistics_id,
        "delivery_info": json.loads(o.delivery_info) if o.delivery_info else {},
        "time_created": o.time_created.isoformat(),
        "time_updated": o.time_updated.isoformat(),
    }
    if include_items:
        out["items"] = [{
            "id": it.id,
            "product_id": it.product_id,
            "product_name": it.product_name,
            "quantity": it.quantity,
            "unit_price": f"{float(it.unit_price):.2f}",
            "subtotal": f"{float(it.subtotal):.2f}",
        } for it in o.items]
    return out


# ───────────────────────── Payments ─────────────────────────

def _resolve_importer_plan(db: Session, user: User) -> Optional[ImporterPlan]:
    """Return the user's current plan (or the default free plan)."""
    plan = None
    if user.plan_id:
        plan = db.get(ImporterPlan, user.plan_id)
    if not plan:
        plan = db.query(ImporterPlan).filter(ImporterPlan.is_default == 1, ImporterPlan.status == 1).first()
    return plan


def _reset_monthly_window_if_needed(user: User) -> None:
    """If the importer's monthly window is over (or never set), reset their counter."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    period = user.monthly_period_start
    if not period or (now - period).days >= 30:
        user.monthly_period_start = now
        user.monthly_spent = 0


@router.post("/payment/init")
@limiter.limit("20/minute")
async def init_payment(
    request: Request,
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    order_id: str = Form(...),
):
    order = db.get(Order, order_id)
    if not order or order.importer_id != user.id:
        raise fail("Order not found", code=404)
    if order.status != "pending":
        raise fail("Payment already initialised for this order")

    # Enforce plan transaction cap. The cap is denominated in the plan's
    # currency; the order may be in a different one (e.g. NGN order vs GBP cap),
    # so we convert via services.fx before comparing. If FX is unavailable for
    # whatever reason we skip enforcement rather than block the user.
    plan = _resolve_importer_plan(db, user)
    if plan and float(plan.transaction_limit) > 0:
        order_in_plan_ccy = fx_convert(float(order.total), order.currency, plan.currency)
        if order_in_plan_ccy is not None:
            _reset_monthly_window_if_needed(user)
            prospective = float(user.monthly_spent or 0) + order_in_plan_ccy
            cap = float(plan.transaction_limit)
            if prospective > cap:
                raise fail(
                    f"This order would exceed your {plan.title} monthly limit "
                    f"({plan.currency} {cap:.2f}; this order ≈ {plan.currency} {order_in_plan_ccy:.2f}). "
                    "Upgrade to Premium for unlimited transactions.",
                    code=402,
                )

    tx_ref = "JARA" + secrets.token_urlsafe(8).replace("-", "")[:12]
    payment = Payment(
        order_id=order.id,
        tx_ref=tx_ref,
        amount=order.total,
        currency=order.currency,
        status="pending",
    )
    db.add(payment)
    db.commit()

    config = build_inline_config(
        tx_ref=tx_ref,
        amount=float(order.total),
        currency=order.currency,
        customer={"email": user.email, "phone_number": user.phone or "", "name": user.fullname},
        order_id=order.order_number,
    )
    return success(config)


@router.get("/payment/verify")
async def verify_pay(
    tx_ref: str = Query(...),
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
):
    payment = db.query(Payment).filter(Payment.tx_ref == tx_ref).first()
    if not payment:
        raise fail("Transaction not found", code=404)
    order = db.get(Order, payment.order_id)
    if not order or order.importer_id != user.id:
        raise fail("Not authorised", code=403)

    flw = await verify_payment(tx_ref)
    if flw.get("status") == "successful":
        payment.status = "successful"
        payment.provider_payload = json.dumps(flw)
        order.status = "paid"

        # Track monthly spend in plan currency for cap enforcement / warnings
        _reset_monthly_window_if_needed(user)
        plan_for_tracking = _resolve_importer_plan(db, user)
        spent_in_plan_ccy = fx_convert(
            float(order.total),
            order.currency,
            plan_for_tracking.currency if plan_for_tracking else order.currency,
        )
        if spent_in_plan_ccy is not None:
            user.monthly_spent = float(user.monthly_spent or 0) + spent_in_plan_ccy

        db.commit()

        # Receipt email
        subject, html = t_payment_invoice(
            user.firstname or "there",
            order.order_number,
            f"{float(order.total):.2f} {order.currency}",
            datetime.now(timezone.utc).strftime("%d %b %Y"),
        )
        send_template(
            db, template="payment_invoice", to=user.email, subject=subject, html=html,
            user_id=user.id, dedupe_key=f"payment_invoice:{tx_ref}",
        )

        # Order status update to buyer
        subject, html = t_order_status_update(user.firstname or "there", order.order_number, "paid")
        send_template(
            db, template="order_status_paid", to=user.email, subject=subject, html=html,
            user_id=user.id, dedupe_key=f"order_status:paid:{order.id}",
        )

        # Approaching-limit warning at 50% / 80%
        plan = _resolve_importer_plan(db, user)
        if plan and float(plan.transaction_limit) > 0:
            cap = float(plan.transaction_limit)
            spent = float(user.monthly_spent or 0)
            pct = round(spent / cap * 100)
            for threshold in (50, 80):
                if pct >= threshold:
                    subject, html = t_transaction_limit_warning(user.firstname or "there", pct, plan.title)
                    send_template(
                        db, template=f"limit_warning_{threshold}", to=user.email, subject=subject, html=html,
                        user_id=user.id,
                        dedupe_key=f"limit_warning:{user.id}:{user.monthly_period_start.date()}:{threshold}",
                    )

        return success({"status": "successful", "tx_ref": tx_ref})
    payment.status = "failed"
    payment.provider_payload = json.dumps(flw)
    db.commit()
    return success({"status": "failed", "tx_ref": tx_ref})


@router.get("/payment")
def transaction_history(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    payments = (
        db.query(Payment)
        .join(Order, Order.id == Payment.order_id)
        .filter(Order.importer_id == user.id)
        .order_by(desc(Payment.time_created))
        .all()
    )
    rows = [{
        "id": p.id,
        "tx_ref": p.tx_ref,
        "order_id": p.order_id,
        "amount": f"{float(p.amount):.2f}",
        "currency": p.currency,
        "status": p.status,
        "time_created": p.time_created.isoformat(),
    } for p in payments]
    return success({"data": rows, "meta": {"paging": {"total": len(rows), "page": 1, "len": len(rows)}}})


# ───────────────────────── Shipping ─────────────────────────

@router.get("/shipping")
def list_shipping(user: User = Depends(require_importer), db: Session = Depends(get_db)):
    rows = db.query(ShippingAddress).filter(ShippingAddress.user_id == user.id).order_by(desc(ShippingAddress.is_default), desc(ShippingAddress.time_created)).all()
    return success([_serialize_shipping(s) for s in rows])


@router.post("/shipping")
def add_shipping(
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    recipient_name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    city: str = Form(...),
    state: Optional[str] = Form(default=None),
    country: str = Form(default="United Kingdom"),
    postal_code: Optional[str] = Form(default=None),
    is_default: int = Form(default=0),
):
    if is_default:
        db.query(ShippingAddress).filter(ShippingAddress.user_id == user.id).update({"is_default": 0})
    addr = ShippingAddress(
        user_id=user.id,
        recipient_name=recipient_name, phone=phone, address=address, city=city, state=state,
        country=country, postal_code=postal_code, is_default=is_default,
    )
    db.add(addr)
    db.commit()
    return success(_serialize_shipping(addr))


@router.patch("/shipping/{shipping_id}")
def update_shipping(
    shipping_id: str,
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    recipient_name: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
    address: Optional[str] = Form(default=None),
    city: Optional[str] = Form(default=None),
    state: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
    postal_code: Optional[str] = Form(default=None),
    is_default: Optional[int] = Form(default=None),
):
    addr = db.get(ShippingAddress, shipping_id)
    if not addr or addr.user_id != user.id:
        raise fail("Address not found", code=404)
    if is_default == 1:
        db.query(ShippingAddress).filter(ShippingAddress.user_id == user.id).update({"is_default": 0})
    for k, v in {"recipient_name": recipient_name, "phone": phone, "address": address, "city": city,
                 "state": state, "country": country, "postal_code": postal_code, "is_default": is_default}.items():
        if v is not None:
            setattr(addr, k, v)
    db.commit()
    return success(_serialize_shipping(addr))


def _serialize_shipping(s: ShippingAddress) -> dict:
    return {
        "id": s.id,
        "recipient_name": s.recipient_name,
        "phone": s.phone,
        "address": s.address,
        "city": s.city,
        "state": s.state,
        "country": s.country,
        "postal_code": s.postal_code,
        "is_default": s.is_default,
    }


# ───────────────────────── Reviews ─────────────────────────

@router.post("/profile/review")
def post_review(
    user: User = Depends(require_importer),
    db: Session = Depends(get_db),
    exporter_id: str = Form(...),
    rating: int = Form(..., ge=1, le=5),
    comment: Optional[str] = Form(default=None),
    order_id: Optional[str] = Form(default=None),
):
    # If tied to an order, ensure it belongs to this importer + that exporter
    if order_id:
        order = db.get(Order, order_id)
        if not order or order.importer_id != user.id or order.exporter_id != exporter_id:
            raise fail("Order not found", code=404)
        if order.status != "delivered":
            raise fail("You can only review delivered orders")
        # One review per order
        if db.query(Review).filter(Review.order_id == order_id, Review.importer_id == user.id).first():
            raise fail("You've already reviewed this order", code=409)

    db.add(Review(importer_id=user.id, exporter_id=exporter_id, order_id=order_id, rating=rating, comment=comment))
    user.review_count = (user.review_count or 0) + 1
    db.commit()

    exporter = db.get(User, exporter_id)
    if exporter:
        link = f"{settings.site_url}/exporter/profile"
        subject, html = t_review_received(exporter.firstname or "there", link)
        send_template(
            db, template="review_received", to=exporter.email, subject=subject, html=html,
            user_id=exporter.id, dedupe_key=f"review_received:{order_id or 'standalone'}:{user.id}:{exporter.id}",
        )
    return success({"posted": True})
