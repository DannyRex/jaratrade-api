"""Currency conversion.

Strategy:
- A static fallback table (so the API works offline / in tests / when the
  upstream rate provider is down).
- Optional live lookups against a public FX API (open.er-api.com - no API key
  required). We cache rates for 6 hours per base currency.

Public surface: just `convert(amount, from_currency, to_currency)`.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import httpx

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


def convert(amount: float, from_currency: str, to_currency: str) -> Optional[float]:
    """Convert `amount` from one ISO-4217 code to another.

    Returns None if neither live nor fallback rates are available - callers
    should treat that as "skip the check" rather than a hard error.
    """
    if amount == 0 or from_currency == to_currency:
        return float(amount)

    # Try live first
    rates = _live_rates(from_currency)
    if rates and to_currency in rates:
        return float(amount) * float(rates[to_currency])

    # Fall back: from -> GBP -> to
    f_to_gbp = _FALLBACK_TO_GBP.get(from_currency)
    t_to_gbp = _FALLBACK_TO_GBP.get(to_currency)
    if f_to_gbp is None or t_to_gbp is None or t_to_gbp == 0:
        return None
    gbp = float(amount) * f_to_gbp
    return gbp / t_to_gbp
