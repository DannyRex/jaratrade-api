"""Platform settings - commission account + commission rate."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..envelope import fail, success
from ..models import Setting, User

router = APIRouter(prefix="/settings", tags=["settings"])

# ───────────────────────── Commission rate ─────────────────────────
# Stored as a numeric percent (e.g. 2.0 means 2%). Read by the Flutterwave
# split builder at payment time and used to route the platform's cut.
COMMISSION_RATE_KEY = "commission_rate"
COMMISSION_RATE_DEFAULT = 2.0  # percent
COMMISSION_RATE_MIN = 0.0
COMMISSION_RATE_MAX = 25.0


def read_commission_rate(db: Session) -> float:
    """Return the configured commission percent (e.g. 2.0 for 2%).

    Defaults to ``COMMISSION_RATE_DEFAULT`` when the setting is missing or
    unparseable. Always clamped to [COMMISSION_RATE_MIN, COMMISSION_RATE_MAX]
    so a misconfigured value can't accidentally route ridiculous splits.
    """
    setting = db.get(Setting, COMMISSION_RATE_KEY)
    if not setting or not setting.value:
        return COMMISSION_RATE_DEFAULT
    try:
        v = float(setting.value)
    except (TypeError, ValueError):
        return COMMISSION_RATE_DEFAULT
    return max(COMMISSION_RATE_MIN, min(COMMISSION_RATE_MAX, v))


@router.get("/commission_rate")
def get_commission_rate(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Returns the current platform commission percent + the effective
    decimal-rate Flutterwave will use in splits."""
    rate = read_commission_rate(db)
    return success({
        "percent": rate,
        "decimal_rate": round(rate / 100, 4),
        "default": COMMISSION_RATE_DEFAULT,
        "min": COMMISSION_RATE_MIN,
        "max": COMMISSION_RATE_MAX,
    })


@router.put("/commission_rate")
def update_commission_rate(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    percent: float = Form(...),
):
    if percent < COMMISSION_RATE_MIN or percent > COMMISSION_RATE_MAX:
        raise fail(
            f"Commission rate must be between {COMMISSION_RATE_MIN}% and {COMMISSION_RATE_MAX}%.",
        )
    setting = db.get(Setting, COMMISSION_RATE_KEY)
    value = f"{percent:.4f}"
    if setting:
        setting.value = value
    else:
        db.add(Setting(key=COMMISSION_RATE_KEY, value=value))
    db.commit()
    return success({
        "percent": percent,
        "decimal_rate": round(percent / 100, 4),
    })


# ───────────────────────── Commission account ─────────────────────────
# Reference record (bank/name/number) shown to staff. Doesn't drive payment
# routing - the Flutterwave subaccount itself is configured via env var.

@router.put("/commision_account")
async def update_commission_account(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    bank_name: str = Form(...),
    account_name: str = Form(...),
    account_number: str = Form(...),
    bank_code: Optional[str] = Form(default=None),
    auto_provision: Optional[bool] = Form(default=False),
):
    """Save the platform commission account.

    If `auto_provision=true` AND `bank_code` is supplied, also provision a
    Flutterwave subaccount on Jaratrade's side and stash the returned
    subaccount_id in the saved record. Subsequent payments use it as the
    commission destination automatically.
    """
    payload: dict = {
        "bank_name": bank_name,
        "account_name": account_name,
        "account_number": account_number,
        "bank_code": bank_code,
    }

    if auto_provision and bank_code:
        from ..services.flutterwave import create_subaccount
        try:
            resp = await create_subaccount(
                account_bank=bank_code,
                account_number=account_number,
                business_name=account_name,
                business_email="admin@jaratrade.com",
                business_mobile="0000000000",
                country="NG",
            )
            sub_id = resp.get("subaccount_id") or resp.get("id")
            if sub_id:
                payload["flw_subaccount_id"] = str(sub_id)
                payload["flw_provisioned_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:  # noqa: BLE001
            payload["flw_provision_error"] = repr(e)

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


def read_commission_subaccount_id(db: Session) -> Optional[str]:
    """Return the Flutterwave subaccount ID we should route commission to,
    preferring the admin-provisioned one over the env var fallback.

    Importer-payment / order-init reads this so the commission split actually
    follows what admin set in the UI.
    """
    from ..config import get_settings as _get_settings

    setting = db.get(Setting, "commission_account")
    if setting and setting.value:
        try:
            data = json.loads(setting.value)
            sub = data.get("flw_subaccount_id")
            if sub:
                return str(sub)
        except (ValueError, TypeError):
            pass
    env_id = _get_settings().flw_commission_subaccount_id
    return env_id or None
