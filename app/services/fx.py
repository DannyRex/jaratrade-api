"""Currency conversion.

Strategy (in order of precedence):
1. Admin-configured override stored in the `Setting` table under keys
   like "fx_rate_NGN_GBP" - lets the team hand-pick a rate without
   redeploying (useful when the live provider is wonky or you want a
   conservative margin against volatility).
2. Live lookups against open.er-api.com (no API key), cached 6h.
3. Static fallback table so tests + offline runs never crash.

Public surface:
  - convert(amount, from, to, db=None) -> Optional[float]
  - current_rate(from, to, db=None)    -> Optional[float]
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import httpx
from sqlalchemy.orm import Session

# Hardcoded fallbacks - illustrative, not authoritative. Refresh quarterly.
# Rates are: 1 unit of `key` -> N units of GBP.
_FALLBACK_TO_GBP: Dict[str, float] = {
    "GBP": 1.0,
    "USD": 0.79,
    "EUR": 0.85,
    "NGN": 0.00050,  # ~₦2,000 per £1
}

# Live cache: { base_currency: (timestamp, {target: rate}) }
_LIVE_CACHE: Dict[str, tuple[float, Dict[str, float]]] = {}
_LIVE_TTL_SECONDS = 6 * 60 * 60


def _live_rates(base: str) -> Optional[Dict[str, float]]:
    """Fetch + cache rates from open.er-api.com. Returns None on any failure."""
    cached = _LIVE_CACHE.get(base)
    if cached and (time.time() - cached[0] < _LIVE_TTL_SECONDS):
        return cached[1]
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"https://open.er-api.com/v6/latest/{base}")
            if r.status_code != 200:
                return None
            body = r.json()
            rates = body.get("rates")
            if not isinstance(rates, dict):
                return None
            _LIVE_CACHE[base] = (time.time(), rates)
            return rates
    except Exception:  # noqa: BLE001
        return None


def _override_rate(db: Optional[Session], from_currency: str, to_currency: str) -> Optional[float]:
    """Read an admin-configured rate from the Setting table, if any.

    Looked-up keys (first hit wins):
      fx_rate_{FROM}_{TO}      e.g. fx_rate_NGN_GBP  (1 NGN = X GBP)
      fx_rate_{TO}_{FROM}      reciprocal — we invert it
    """
    if db is None:
        return None
    try:
        from ..models import Setting  # local import to avoid circular at module load
    except Exception:  # noqa: BLE001
        return None

    key = f"fx_rate_{from_currency.upper()}_{to_currency.upper()}"
    s = db.get(Setting, key)
    if s and s.value:
        try:
            v = float(s.value)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    key_rev = f"fx_rate_{to_currency.upper()}_{from_currency.upper()}"
    s = db.get(Setting, key_rev)
    if s and s.value:
        try:
            v = float(s.value)
            if v > 0:
                return 1.0 / v
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def current_rate(from_currency: str, to_currency: str, db: Optional[Session] = None) -> Optional[float]:
    """Return the multiplier such that `amount_to = amount_from * rate`.

    Returns None if no rate is available.
    """
    if from_currency == to_currency:
        return 1.0

    # 1. Admin override
    override = _override_rate(db, from_currency, to_currency)
    if override is not None:
        return override

    # 2. Live
    rates = _live_rates(from_currency)
    if rates and to_currency in rates:
        return float(rates[to_currency])

    # 3. Fallback table via GBP
    f_to_gbp = _FALLBACK_TO_GBP.get(from_currency)
    t_to_gbp = _FALLBACK_TO_GBP.get(to_currency)
    if f_to_gbp is None or t_to_gbp is None or t_to_gbp == 0:
        return None
    return f_to_gbp / t_to_gbp


def convert(amount: float, from_currency: str, to_currency: str, db: Optional[Session] = None) -> Optional[float]:
    """Convert `amount` from one ISO-4217 code to another.

    Returns None if neither live nor fallback rates are available - callers
    should treat that as "skip the check" rather than a hard error.
    """
    if amount == 0 or from_currency == to_currency:
        return float(amount)
    rate = current_rate(from_currency, to_currency, db=db)
    if rate is None:
        return None
    return float(amount) * rate
