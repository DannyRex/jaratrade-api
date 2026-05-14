"""Platform settings - currently the commission account configuration."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import success
from ..models import Setting, User

router = APIRouter(prefix="/settings", tags=["settings"])


@router.put("/commision_account")
def update_commission_account(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    bank_name: str = Form(...),
    account_name: str = Form(...),
    account_number: str = Form(...),
):
    payload = {"bank_name": bank_name, "account_name": account_name, "account_number": account_number}
    setting = db.get(Setting, "commission_account")
    if setting:
        setting.value = json.dumps(payload)
    else:
        db.add(Setting(key="commission_account", value=json.dumps(payload)))
    db.commit()
    return success(payload)


@router.get("/commision_account")
def get_commission_account(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    setting = db.get(Setting, "commission_account")
    return success(json.loads(setting.value) if setting and setting.value else {})
