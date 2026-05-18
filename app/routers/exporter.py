"""Exporter-scoped endpoints - products CRUD, stores, orders, profile."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_exporter
from ..envelope import fail, success
from ..models import Order, OrderItem, Product, Store, User
from ..models.user import BusinessProfile
from ..routers.public import _serialize_product_summary
from ..security import hash_password, verify_password
from ..services.cloudinary import upload_file
from ..services.email import send_template, t_order_status_update

router = APIRouter(prefix="/exp", tags=["exporter"])


# ───────────────────────── Profile ─────────────────────────

@router.get("/profile")
def get_profile(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    biz = user.business
    total_orders = db.query(Order).filter(Order.exporter_id == user.id).count()
    pending_orders = db.query(Order).filter(Order.exporter_id == user.id, Order.status.in_(["pending", "paid", "confirmed", "preparing"])).count()
    total_revenue = sum(float(o.total) for o in db.query(Order).filter(Order.exporter_id == user.id, Order.status.in_(["delivered"])).all())

    orders = (
        db.query(Order)
        .filter(Order.exporter_id == user.id)
        .order_by(desc(Order.time_created))
        .limit(20)
        .all()
    )

    return success({
        "id": user.id,
        "firstname": user.firstname,
        "lastname": user.lastname,
        "phone": user.phone,
        "email": user.email,
        "address": user.address,
        "country": user.country,
        "profile_name": user.profile_name,
        "business_name": biz.business_name if biz else None,
        "business_email": biz.business_email if biz else None,
        "business_address": biz.business_address if biz else None,
        "business_country": biz.business_country if biz else None,
        "business_reg_number": biz.business_reg_number if biz else None,
        "business_type": biz.business_type if biz else None,
        "annual_turnover": biz.annual_turnover if biz else None,
        "duration_in_business": biz.duration_in_business if biz else None,
        "tin": biz.tin if biz else None,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "total_revenue": f"{total_revenue:.2f}",
        "orders": [{
            "id": o.id,
            "order_id": o.order_number,
            "total": f"{float(o.total):.2f}",
            "currency": o.currency,
            "status": o.status,
            "time_created": o.time_created.isoformat(),
        } for o in orders],
    })


@router.post("/profile")
def update_profile(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    firstname: Optional[str] = Form(default=None),
    lastname: Optional[str] = Form(default=None),
    phone: Optional[str] = Form(default=None),
    address: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
    profile_name: Optional[str] = Form(default=None),
    business_name: Optional[str] = Form(default=None),
    business_email: Optional[str] = Form(default=None),
    business_address: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
):
    for k, v in {"firstname": firstname, "lastname": lastname, "phone": phone, "address": address,
                 "country": country, "profile_name": profile_name}.items():
        if v is not None:
            setattr(user, k, v)
    if user.business:
        for k, v in {"business_name": business_name, "business_email": business_email, "business_address": business_address}.items():
            if v is not None:
                setattr(user.business, k, v)
    db.commit()
    return success({"updated": True})


@router.post("/change_password")
def change_password(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    old_password: str = Form(...),
    new_password: str = Form(..., min_length=8),
):
    if not verify_password(old_password, user.password_hash):
        raise fail("Current password is incorrect", code=401)
    user.password_hash = hash_password(new_password)
    db.commit()
    return success({"changed": True})


# ───────────────────────── Stores ─────────────────────────

@router.get("/store")
def list_stores(user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    stores = db.query(Store).filter(Store.exporter_id == user.id).all()
    rows = [_serialize_store(s, db) for s in stores]
    return success({"data": rows, "meta": {"paging": {"total": len(rows), "page": 1, "len": len(rows)}}})


@router.put("/store")
def create_store(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    market_id: str = Form(...),
    address: str = Form(...),
):
    store = Store(exporter_id=user.id, market_id=market_id, address=address)
    db.add(store)
    db.commit()
    return success(_serialize_store(store, db))


@router.delete("/store/{store_id}")
def delete_store(store_id: str, user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    store = db.get(Store, store_id)
    if not store or store.exporter_id != user.id:
        raise fail("Store not found", code=404)
    db.delete(store)
    db.commit()
    return success({"deleted": True})


def _serialize_store(s: Store, db: Session) -> dict:
    market = s.market or db.get(__import__("app.models", fromlist=["Market"]).Market, s.market_id)
    return {
        "id": s.id,
        "market_id": s.market_id,
        "market_name": market.name if market else "",
        "address": s.address,
        "city": market.city if market else "",
        "state": market.state if market else "",
        "is_default": s.is_default,
        "status": s.status,
    }


# ───────────────────────── Products ─────────────────────────

@router.get("/product")
def list_products(user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    items = db.query(Product).filter(Product.exporter_id == user.id).order_by(desc(Product.time_created)).all()
    rows = [_serialize_product_summary(p, db) for p in items]
    return success({"data": rows, "meta": {"paging": {"total": len(rows), "page": 1, "len": len(rows)}}})


@router.put("/product")
def create_product(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    product_name: str = Form(...),
    description: str = Form(...),
    category_id: str = Form(...),
    store_id: str = Form(...),
    price: float = Form(..., gt=0),
    currency: str = Form(default="NGN"),
    min_order_quantity: int = Form(default=1, ge=1),
    max_order_quantity: int = Form(default=0, ge=0),
    properties: Optional[str] = Form(default="{}"),
    short_video_link: Optional[str] = Form(default=None),
):
    store = db.get(Store, store_id)
    if not store or store.exporter_id != user.id:
        raise fail("Store does not belong to you", code=403)
    prod = Product(
        exporter_id=user.id,
        store_id=store_id,
        category_id=category_id,
        product_name=product_name,
        description=description,
        price=price,
        currency=currency,
        min_order_quantity=min_order_quantity,
        max_order_quantity=max_order_quantity,
        properties=properties or "{}",
        short_video_link=short_video_link,
        images="[]",
        status=1,
    )
    db.add(prod)
    db.commit()
    return success({"id": prod.id, "product_name": prod.product_name})


@router.patch("/product/{product_id}")
def update_product(
    product_id: str,
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    product_name: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    price: Optional[float] = Form(default=None, gt=0),
    min_order_quantity: Optional[int] = Form(default=None, ge=1),
    max_order_quantity: Optional[int] = Form(default=None, ge=0),
    stock_quantity: Optional[int] = Form(default=None, ge=0),
    low_stock_threshold: Optional[int] = Form(default=None, ge=0),
    status: Optional[int] = Form(default=None, ge=0, le=1),
):
    from datetime import datetime as _dt, timezone as _tz

    prod = db.get(Product, product_id)
    if not prod or prod.exporter_id != user.id:
        raise fail("Product not found", code=404)
    fields = {
        "product_name": product_name,
        "description": description,
        "price": price,
        "min_order_quantity": min_order_quantity,
        "max_order_quantity": max_order_quantity,
        "stock_quantity": stock_quantity,
        "low_stock_threshold": low_stock_threshold,
        "status": status,
    }
    for k, v in fields.items():
        if v is not None:
            setattr(prod, k, v)
    # Any update to stock or price counts as an inventory refresh - bumps search ranking.
    if stock_quantity is not None or price is not None:
        prod.last_inventory_update_at = _dt.now(_tz.utc).replace(tzinfo=None)
    db.commit()
    return success({"updated": True})


@router.post("/product/{product_id}/confirm-inventory")
def confirm_inventory(
    product_id: str,
    stock_quantity: Optional[int] = Form(default=None, ge=0),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    """Mark this product's stock as freshly confirmed.

    Per BRD: exporters must confirm or update inventory at least weekly.
    Confirmed-fresh products are prioritised in search.
    """
    from datetime import datetime as _dt, timezone as _tz

    prod = db.get(Product, product_id)
    if not prod or prod.exporter_id != user.id:
        raise fail("Product not found", code=404)
    if stock_quantity is not None:
        prod.stock_quantity = stock_quantity
    prod.last_inventory_update_at = _dt.now(_tz.utc).replace(tzinfo=None)
    db.commit()
    return success({"confirmed": True, "stock_quantity": prod.stock_quantity})


@router.post("/product/confirm-inventory-all")
def confirm_inventory_all(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    """Bulk-confirm every active product's inventory in one click. Doesn't touch
    stock levels - just refreshes the timestamp."""
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc).replace(tzinfo=None)
    rows = db.query(Product).filter(Product.exporter_id == user.id, Product.status == 1).all()
    for p in rows:
        p.last_inventory_update_at = now
    db.commit()
    return success({"confirmed": len(rows)})


@router.delete("/product/{product_id}")
def delete_product(product_id: str, user: User = Depends(require_exporter), db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod or prod.exporter_id != user.id:
        raise fail("Product not found", code=404)
    db.delete(prod)
    db.commit()
    return success({"deleted": True})


@router.post("/product/image/{product_id}")
async def add_product_images(
    product_id: str,
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    images: List[UploadFile] = File(...),
):
    prod = db.get(Product, product_id)
    if not prod or prod.exporter_id != user.id:
        raise fail("Product not found", code=404)
    existing = json.loads(prod.images or "[]")
    for img in images:
        content = await img.read()
        url = await upload_file(content, img.filename or "image.png", folder="products")
        if url:
            existing.append(url)
    prod.images = json.dumps(existing)
    db.commit()
    return success({"images": existing})


@router.delete("/product/image/{product_id}")
def delete_product_image(
    product_id: str,
    image_path: str = Query(...),
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    prod = db.get(Product, product_id)
    if not prod or prod.exporter_id != user.id:
        raise fail("Product not found", code=404)
    images = [u for u in json.loads(prod.images or "[]") if u != image_path]
    prod.images = json.dumps(images)
    db.commit()
    return success({"images": images})


# ───────────────────────── Order updates ─────────────────────────

_ALLOWED_STATUS_TRANSITIONS = {
    # "confirmed" and "preparing" are optional bookkeeping states; exporters
    # can skip straight from paid -> shipped if they want.
    "paid": {"confirmed", "preparing", "shipped", "cancelled"},
    "confirmed": {"preparing", "shipped", "cancelled"},
    "preparing": {"shipped", "cancelled"},
    "shipped": {"delivered", "cancelled"},
    "delivered": set(),  # terminal from exporter side; buyer may still confirm receipt
    "cancelled": set(),
    "failed": set(),
    "refunded": set(),
}


@router.post("/update_order")
def update_order_status(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    order_id: str = Form(...),
    status: str = Form(...),
):
    """Move an order along its lifecycle and notify the buyer.

    Side effects:
      - `order.status` is updated.
      - `order.time_updated` is stamped (the payout cron's 7-day dispute
        window measures from this timestamp once status == "delivered").
      - A status-change email goes to the importer.
    """
    order = db.get(Order, order_id)
    if not order or order.exporter_id != user.id:
        raise fail("Order not found", code=404)

    status = (status or "").strip().lower()
    allowed = _ALLOWED_STATUS_TRANSITIONS.get(order.status, set())
    if status not in allowed and status != order.status:
        raise fail(
            f"Cannot move order from '{order.status}' to '{status}'",
            code=400,
        )

    order.status = status
    order.time_updated = datetime.now(timezone.utc)
    db.commit()

    # Notify the importer. Failures here shouldn't roll back the status change.
    importer = db.get(User, order.importer_id)
    if importer and importer.email:
        try:
            subject, html = t_order_status_update(
                importer.firstname or "there",
                order.order_number,
                status,
            )
            send_template(
                db,
                template=f"order_status_{status}",
                to=importer.email,
                subject=subject,
                html=html,
                user_id=importer.id,
                dedupe_key=f"order_status:{status}:{order.id}",
            )
        except Exception:  # noqa: BLE001
            # Email is best-effort; don't fail the status change for SMTP blips.
            import traceback as _tb
            _tb.print_exc()

    return success({"status": status})
