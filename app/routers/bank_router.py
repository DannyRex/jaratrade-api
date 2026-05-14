"""Bank update/delete (kept at /bank/:id to mirror the legacy contract)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import Bank, User
from ..routers.public import _serialize_bank

router = APIRouter(prefix="/bank", tags=["banks"])


@router.patch("/{bank_id}")
def update_bank(
    bank_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: Optional[str] = Form(default=None),
    country: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    paystack_code: Optional[str] = Form(default=None),
    flutter_code: Optional[str] = Form(default=None),
):
    b = db.get(Bank, bank_id)
    if not b:
        raise fail("Bank not found", code=404)
    for k, v in {"name": name, "country": country, "description": description,
                 "paystack_code": paystack_code, "flutter_code": flutter_code}.items():
        if v is not None:
            setattr(b, k, v)
    db.commit()
    return success(_serialize_bank(b))


@router.delete("/{bank_id}")
def delete_bank(bank_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    b = db.get(Bank, bank_id)
    if not b:
        raise fail("Bank not found", code=404)
    db.delete(b)
    db.commit()
    return success({"deleted": True})
