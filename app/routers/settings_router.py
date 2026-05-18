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


# ───────────────────────── FX rate (display + secondary price) ─────────────────
# Stored as the multiplier from one currency to another, e.g. for NGN -> GBP
# you'd save 0.00057 (= 1 / 1750 if 1 GBP ≈ 1,750 NGN). Mostly used so the
# marketplace can show a UK buyer "₦18,000 / ~£10" next to the price without
# hitting the live FX API on every page render.

@router.get("/fx_rate")
def get_fx_rate(
    from_currency: str = "NGN",
    to_currency: str = "GBP",
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return the current effective rate between the two currencies, the
    admin-override value (if any), and live + fallback values for context."""
    from ..services.fx import _FALLBACK_TO_GBP, _live_rates, _override_rate, current_rate

    pair_from = from_currency.upper()
    pair_to = to_currency.upper()
    override = _override_rate(db, pair_from, pair_to)
    live_rates = _live_rates(pair_from)
    live = live_rates.get(pair_to) if live_rates else None
    f_to_gbp = _FALLBACK_TO_GBP.get(pair_from)
    t_to_gbp = _FALLBACK_TO_GBP.get(pair_to)
    fallback = (f_to_gbp / t_to_gbp) if f_to_gbp is not None and t_to_gbp not in (None, 0) else None
    effective = current_rate(pair_from, pair_to, db=db)
    return success({
        "from": pair_from,
        "to": pair_to,
        "effective_rate": effective,
        "override_rate": override,
        "live_rate": live,
        "fallback_rate": fallback,
        # Convenience: what does 1000 units convert to?
        "example_1000": (effective * 1000) if effective else None,
    })


@router.put("/fx_rate")
def update_fx_rate(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    rate: float = Form(...),
):
    """Save an admin-configured FX rate override. Takes effect immediately
    for all secondary-price displays + plan-currency conversion checks."""
    pair_from = from_currency.upper()
    pair_to = to_currency.upper()
    if pair_from == pair_to:
        raise fail("from and to must differ")
    if rate <= 0:
        raise fail("rate must be positive")
    key = f"fx_rate_{pair_from}_{pair_to}"
    setting = db.get(Setting, key)
    if setting:
        setting.value = f"{rate:.10f}"
    else:
        db.add(Setting(key=key, value=f"{rate:.10f}"))
    db.commit()
    return success({"from": pair_from, "to": pair_to, "rate": rate})


@router.delete("/fx_rate")
def clear_fx_rate(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    from_currency: str = "NGN",
    to_currency: str = "GBP",
):
    """Clear the override so the live/fallback rate kicks back in."""
    key = f"fx_rate_{from_currency.upper()}_{to_currency.upper()}"
    setting = db.get(Setting, key)
    if setting:
        db.delete(setting)
        db.commit()
    return success({"cleared": True})


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
        from ..services.flutterwave import FlutterwaveError, create_subaccount
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
        except FlutterwaveError as e:
            payload["flw_provision_error"] = f"{e.status_code}: {e.body}"
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
