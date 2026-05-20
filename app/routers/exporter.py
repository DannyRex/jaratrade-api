"""Exporter-scoped endpoints - products CRUD, stores, orders, profile."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..config import get_settings
from ..constants import ROLE_ADMIN
from ..database import get_db
from ..deps import require_exporter
from ..envelope import fail, success
from ..models import Order, OrderItem, Product, Store, User
from ..models.user import BusinessProfile
from ..routers.public import _serialize_product_summary
from ..security import hash_password, verify_password
from ..services.cloudinary import upload_file
from ..services.email import (
    send_template,
    t_account_under_review,
    t_new_exporter_pending_review_admin,
    t_order_status_update,
)

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
        "valid_identification": biz.valid_identification if biz else None,
        "bank_id": biz.bank_id if biz else None,
        "account_number": biz.account_number if biz else None,
        # Uploaded KYC documents - {doc_type: url}.
        "documents": biz.documents_dict if biz else {},
        # KYC lifecycle - drives the "Submit for review" UI.
        "kyc_status": user.kyc_status,
        "kyc_submitted_at": user.kyc_submitted_at.isoformat() if user.kyc_submitted_at else None,
        "kyc_rejection_reason": user.kyc_rejection_reason,
        # Empty list = profile complete + ready to submit.
        "kyc_missing_fields": _kyc_missing_fields(user),
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
    # KYC detail fields - collected post-signup on the slim-signup flow.
    # These are exactly what the /exp/submit-for-review completeness check
    # requires, so the exporter must be able to set them here.
    business_reg_num: Optional[str] = Form(default=None),
    business_type: Optional[str] = Form(default=None),
    business_country: Optional[str] = Form(default=None),
    annual_turnover: Optional[str] = Form(default=None),
    duration_in_business: Optional[str] = Form(default=None),
    tin: Optional[str] = Form(default=None),
    valid_ID: Optional[str] = Form(default=None),
    bank_id: Optional[str] = Form(default=None),
    account_name: Optional[str] = Form(default=None),
    account_number: Optional[str] = Form(default=None),
):
    for k, v in {"firstname": firstname, "lastname": lastname, "phone": phone, "address": address,
                 "country": country, "profile_name": profile_name}.items():
        if v is not None:
            setattr(user, k, v)

    try:
        duration_int = int(duration_in_business) if duration_in_business else None
    except (TypeError, ValueError):
        duration_int = None

    # Full set of BusinessProfile columns the exporter can edit. Map of
    # column name -> submitted value; only non-None values are applied.
    biz_updates = {
        "business_name": business_name,
        "business_email": business_email,
        "business_address": business_address,
        "business_reg_number": business_reg_num,
        "business_type": business_type,
        "business_country": business_country,
        "annual_turnover": annual_turnover,
        "duration_in_business": duration_int,
        "tin": tin,
        "valid_identification": valid_ID,
        "bank_id": bank_id,
        "account_name": account_name,
        "account_number": account_number,
    }
    # Lazy-create the BusinessProfile: slim-signup exporters won't have
    # one yet, but the moment they fill in business details from their
    # profile screen we create the row. business_name is the only
    # NOT NULL field on the table, so it's the gate.
    if user.business:
        for k, v in biz_updates.items():
            if v is not None:
                setattr(user.business, k, v)
    elif business_name:
        db.add(BusinessProfile(
            user_id=user.id,
            **{k: v for k, v in biz_updates.items() if v is not None},
        ))
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


# ───────────────────────── KYC submission ─────────────────────────

# Document slots the exporter can upload as KYC proof.
_KYC_DOC_TYPES = {"id", "cac"}
# Reject obviously-wrong uploads early. Images + PDF cover scans/photos.
_KYC_DOC_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "pdf", "heic"}


@router.post("/kyc-document")
async def upload_kyc_document(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
    doc_type: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a KYC proof document.

    doc_type:
      - "id"  : means-of-ID scan (passport / NIN slip / driver's licence)
      - "cac" : business registration (CAC) certificate

    The file goes to Cloudinary; its URL is merged into
    BusinessProfile.documents, a JSON dict {doc_type: url}. The means-of-ID
    document is what the /exp/submit-for-review completeness check requires
    - a free-text "I have a passport" proves nothing to a KYC reviewer.
    """
    doc_type = (doc_type or "").strip().lower()
    if doc_type not in _KYC_DOC_TYPES:
        raise fail(f"doc_type must be one of: {', '.join(sorted(_KYC_DOC_TYPES))}", code=400)

    filename = file.filename or f"{doc_type}.pdf"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _KYC_DOC_EXTENSIONS:
        raise fail(
            "Unsupported file type. Upload an image (jpg/png/webp/heic) or a PDF.",
            code=400,
        )

    content = await file.read()
    if not content:
        raise fail("The uploaded file is empty.", code=400)
    # Cloudinary free tier caps at 10MB for raw/image; keep a sane bound.
    if len(content) > 10 * 1024 * 1024:
        raise fail("File too large - keep KYC documents under 10MB.", code=400)

    url = await upload_file(content, filename, folder="kyc")
    if not url:
        raise fail("Upload failed - please try again.", code=502)

    biz = user.business
    if not biz:
        # Lazy-create. business_name is NOT NULL but an empty string
        # satisfies that; the completeness check treats "" as missing so
        # this doesn't falsely mark the profile ready.
        biz = BusinessProfile(user_id=user.id, business_name="")
        db.add(biz)
        db.flush()

    docs = biz.documents_dict
    docs[doc_type] = url
    biz.documents = json.dumps(docs)
    db.commit()
    return success({"doc_type": doc_type, "url": url, "documents": docs})


# ───────────────────────── KYC submission ─────────────────────────

# Fields an exporter must have on file before they can submit for review.
# Each tuple is (human label, getter). bank_id + account_number are
# required because the KYC-approval step provisions the Flutterwave
# subaccount, which can't be created without the seller's bank details.
def _kyc_missing_fields(user: User) -> List[str]:
    """Return a list of human-readable missing items, empty if complete."""
    missing: List[str] = []
    if not (user.firstname and user.lastname):
        missing.append("Your name")
    if not user.phone:
        missing.append("Phone number")
    biz = user.business
    if not biz:
        # No business profile at all - everything below is missing.
        return missing + [
            "Business name", "Business registration (CAC) number",
            "Business address", "Tax ID (TIN)", "Means of ID document",
            "Bank account",
        ]
    if not biz.business_name:
        missing.append("Business name")
    if not biz.business_reg_number:
        missing.append("Business registration (CAC) number")
    if not biz.business_address:
        missing.append("Business address")
    if not biz.tin:
        missing.append("Tax ID (TIN)")
    # Means of ID is now an uploaded document, not free text. The proof is
    # the file in documents["id"], not the valid_identification string.
    if not biz.documents_dict.get("id"):
        missing.append("Means of ID document")
    if not (biz.bank_id and biz.account_number):
        missing.append("Bank account (bank + account number)")
    return missing


@router.post("/submit-for-review")
def submit_for_review(
    user: User = Depends(require_exporter),
    db: Session = Depends(get_db),
):
    """Exporter hands their completed business profile to admin for KYC review.

    This is the gate between "signed up" and "in the admin review queue".
    Until an exporter calls this, kyc_submitted_at is NULL and they don't
    appear in /adm/kyc/queue - so an admin can't approve an empty profile.

    Guards:
      - already approved -> nothing to do
      - profile incomplete -> 400 with the list of missing items
    """
    if user.kyc_status == "approved":
        raise fail("Your account is already approved.", code=400)

    missing = _kyc_missing_fields(user)
    if missing:
        raise fail(
            "Complete your profile before submitting: " + "; ".join(missing),
            code=400,
        )

    # Stamp the submission. If they were previously rejected and are
    # re-submitting after fixing the issues, flip them back into the
    # pending queue.
    user.kyc_submitted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if user.kyc_status == "rejected":
        user.kyc_status = "pending"
        user.kyc_rejection_reason = None
    db.commit()

    # Notify the applicant that their submission is in the queue.
    try:
        subject, html = t_account_under_review(user.firstname or "there")
        send_template(
            db,
            template="account_under_review",
            to=user.email,
            subject=subject,
            html=html,
            user_id=user.id,
            dedupe_key=f"under_review:{user.id}:{user.kyc_submitted_at.isoformat()}",
        )
    except Exception:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()

    # Notify every active admin that there's a new application to review.
    try:
        s = get_settings()
        review_link = f"{s.site_url}/admin/kyc"
        biz = user.business
        for admin in db.query(User).filter(
            User.role == ROLE_ADMIN, User.is_active.is_(True)
        ).all():
            if not admin.email:
                continue
            subj, ahtml = t_new_exporter_pending_review_admin(
                business_name=biz.business_name if biz else "",
                business_email=(biz.business_email if biz else "") or "",
                contact_name=f"{user.firstname} {user.lastname}".strip() or user.email,
                contact_email=user.email,
                review_link=review_link,
            )
            send_template(
                db,
                template="new_exporter_pending_review_admin",
                to=admin.email,
                subject=subj,
                html=ahtml,
                user_id=admin.id,
                dedupe_key=f"new_exporter_pending:{user.id}:{user.kyc_submitted_at.isoformat()}:{admin.id}",
            )
    except Exception:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()

    return success({
        "kyc_status": user.kyc_status,
        "kyc_submitted_at": user.kyc_submitted_at.isoformat(),
    }, message="Submitted for review. We'll email you once an admin has looked it over.")


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
