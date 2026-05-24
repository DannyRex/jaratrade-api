"""Flutterwave integration.

Two responsibilities:
1. Build the Inline-checkout config the frontend needs to launch the modal.
2. Verify a payment by tx_ref against Flutterwave's REST API.

We deliberately don't redirect - the frontend uses Flutterwave's `Inline JS`
which expects a config object with public_key, tx_ref, amount, etc. We return
that same shape from `/imp/payment/init`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from ..config import get_settings

settings = get_settings()


_DEFAULT_PAYMENT_OPTIONS = "card,banktransfer,ussd"


def build_inline_config(
    *,
    tx_ref: str,
    amount: float,
    currency: str,
    customer: Dict[str, str],
    order_id: str,
    commission_rate: Optional[float] = None,
    commission_subaccount_id: Optional[str] = None,
    seller_subaccount_id: Optional[str] = None,
    payment_options: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the Flutterwave Inline-checkout config.

    Three-way split semantics (per Flutterwave's `subaccounts` array):
      - The seller's subaccount receives (100% - commission)% of the order.
      - The commission subaccount receives `commission_rate`.
      - Anything left in the merchant wallet is automatically Jaratrade's.

    If `seller_subaccount_id` is missing we fall back to a single-party
    split routing only the commission to the platform - which is what
    payment-flow looked like before subaccount provisioning. That keeps
    legacy / dev paths functional while we onboard sellers.

    `commission_rate` is the decimal (e.g. 0.02 for 2%). When None we
    default to 0.02 for backwards compatibility.
    """
    rate = 0.02 if commission_rate is None else commission_rate
    commission_id = commission_subaccount_id or settings.flw_commission_subaccount_id

    subaccounts = []
    # Seller share first (largest); commission second.
    if seller_subaccount_id:
        subaccounts.append({
            "id": seller_subaccount_id,
            "transaction_split_ratio": round((1.0 - rate) * 100, 4),
            "transaction_charge_type": "percentage",
        })
    if commission_id:
        subaccounts.append({
            "id": commission_id,
            "transaction_split_ratio": round(rate * 100, 4),
            "transaction_charge_type": "percentage",
        })

    # Legacy `split` field kept for backwards compat with merchants who
    # haven't migrated their inline integration. Flutterwave honours both
    # but `subaccounts` is the newer canonical form.
    split = []
    if commission_id and not seller_subaccount_id:
        split.append({
            "id": commission_id,
            "transaction_charge_type": "percentage",
            "transaction_charge": f"{rate:.4f}",
        })
    return {
        "public_key": settings.flw_public_key,
        "tx_ref": tx_ref,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "payment_options": payment_options or _DEFAULT_PAYMENT_OPTIONS,
        "customer": customer,
        "customizations": {
            "title": "Jaratrade",
            "description": f"Payment for order {order_id}",
            "logo": False,
        },
        "split": split,
        "subaccounts": subaccounts,
    }


async def create_standard_payment(
    *,
    tx_ref: str,
    amount: float,
    currency: str,
    customer: Dict[str, str],
    order_id: str,
    redirect_url: str,
    commission_rate: Optional[float] = None,
    commission_subaccount_id: Optional[str] = None,
    seller_subaccount_id: Optional[str] = None,
    payment_options: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Flutterwave Standard hosted-checkout session.

    Unlike Inline (which embeds FLW's v3.js into our page and renders a
    modal), Standard returns a hosted URL the user is redirected to. FLW
    owns the entire payment page - no script tag for ad-blockers / CDN
    flakiness / CSP / browser extensions to interfere with.

    Returns:  {"link": "https://checkout.flutterwave.com/<hash>"}
    On error: raises FlutterwaveError with parsed response body.
    """
    rate = 0.02 if commission_rate is None else commission_rate
    commission_id = commission_subaccount_id or settings.flw_commission_subaccount_id

    subaccounts = []
    if seller_subaccount_id:
        subaccounts.append({
            "id": seller_subaccount_id,
            "transaction_split_ratio": round((1.0 - rate) * 100, 4),
            "transaction_charge_type": "percentage",
        })
    if commission_id:
        subaccounts.append({
            "id": commission_id,
            "transaction_split_ratio": round(rate * 100, 4),
            "transaction_charge_type": "percentage",
        })

    payload: Dict[str, Any] = {
        "tx_ref": tx_ref,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "redirect_url": redirect_url,
        "payment_options": payment_options or _DEFAULT_PAYMENT_OPTIONS,
        "customer": customer,
        "customizations": {
            "title": "Jaratrade",
            "description": f"Payment for order {order_id}",
        },
        "meta": {"order_id": order_id},
    }
    if subaccounts:
        payload["subaccounts"] = subaccounts

    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.post("https://api.flutterwave.com/v3/payments", json=payload)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise FlutterwaveError(resp.status_code, body)
        body = resp.json()
    data = body.get("data") or {}
    link = data.get("link")
    if not link:
        raise FlutterwaveError(502, f"FLW response missing 'link': {body}")
    return {"link": link, "tx_ref": tx_ref}


async def verify_payment(tx_ref: str) -> Dict[str, Any]:
    """Look up a transaction by tx_ref. Returns Flutterwave's data block.

    The Flutterwave data block includes a `card` object with `token` when the
    payment was a card transaction - we capture it on the subscription record
    for tokenized auto-recharge on renewal.
    """
    if not settings.flw_secret_key:
        # Dev fallback: pretend it succeeded so the full flow can be exercised locally.
        return {
            "status": "successful",
            "tx_ref": tx_ref,
            "amount": 0,
            "currency": "NGN",
            "card": {"token": "DEV-CARD-TOKEN", "last_4digits": "4242", "type": "visa"},
            "_dev_fallback": True,
        }

    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(
            "https://api.flutterwave.com/v3/transactions/verify_by_reference",
            params={"tx_ref": tx_ref},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("data") or {}


async def tokenized_charge(
    *,
    token: str,
    amount: float,
    currency: str,
    tx_ref: str,
    email: str,
    customer_name: str,
) -> Dict[str, Any]:
    """Charge a stored card token. Used by the subscription renewal cron so
    the user doesn't have to re-enter their card every period.

    Returns Flutterwave's `data` block, or a dev-stub when no secret is set.
    """
    if not settings.flw_secret_key:
        return {
            "status": "successful",
            "tx_ref": tx_ref,
            "amount": amount,
            "currency": currency,
            "_dev_fallback": True,
        }

    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    payload = {
        "token": token,
        "currency": currency,
        "country": "NG",
        "amount": float(amount),
        "email": email,
        "tx_ref": tx_ref,
        "fullname": customer_name or email,
    }
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.post("https://api.flutterwave.com/v3/tokenized-charges", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data") or {"status": "failed", "raw": body}


async def resolve_account(*, account_number: str, account_bank: str) -> Dict[str, Any]:
    """Verify a Nigerian bank account number resolves to a real account name.

    Used before subaccount creation to catch typos / bad account numbers. The
    Flutterwave endpoint is POST /v3/accounts/resolve. Returns the resolved
    `account_name` (or raises on failure). Dev-fallback echoes the inputs.
    """
    if not settings.flw_secret_key:
        return {
            "account_number": account_number,
            "account_bank": account_bank,
            "account_name": "DEV ACCOUNT NAME",
            "_dev_fallback": True,
        }
    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    payload = {"account_number": account_number, "account_bank": account_bank}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.post("https://api.flutterwave.com/v3/accounts/resolve", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data") or {"status": "failed", "raw": body}


# ISO-3166-1 alpha-2 codes are what Flutterwave wants. We normalise common
# free-form country strings the rest of our app uses ("Nigeria", "United
# Kingdom", etc) into these codes before hitting FLW. Unknown values are
# passed through untouched so they surface as a clear FLW error rather than
# silently mapping to the wrong country.
_COUNTRY_ISO = {
    "nigeria": "NG", "ng": "NG",
    "united kingdom": "GB", "uk": "GB", "gb": "GB",
    "united states": "US", "usa": "US", "us": "US",
    "ghana": "GH", "kenya": "KE", "south africa": "ZA",
}


def _iso_country(value: Optional[str]) -> str:
    if not value:
        return "NG"
    key = value.strip().lower()
    if len(key) == 2:
        return key.upper()
    return _COUNTRY_ISO.get(key, value)


class FlutterwaveError(RuntimeError):
    """Raised when Flutterwave returns a non-2xx. Carries the parsed response
    body so callers can surface a useful error to the admin."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        # Build a short, useful summary message - prefer FLW's `message`
        # field over the raw text dump.
        msg = body.get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"Flutterwave {status_code}: {msg}")


async def _flw_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to Flutterwave's API with our standard auth + error wrapping.

    Raises FlutterwaveError with the parsed body on non-2xx. Returns the
    `data` block from the response on success.
    """
    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.post(f"https://api.flutterwave.com{path}", json=payload)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise FlutterwaveError(resp.status_code, body)
        body = resp.json()
        return body.get("data") or {"status": body.get("status"), "raw": body}


async def create_subaccount(
    *,
    account_bank: str,
    account_number: str,
    business_name: str,
    business_email: str,
    business_mobile: str,
    country: str = "NG",
    split_value: float = 1.0,
    split_type: str = "percentage",
) -> Dict[str, Any]:
    """Provision a Flutterwave subaccount.

    Returns Flutterwave's `data` block; `data.subaccount_id` is the public
    ID we store on our User row + reference in payment-time splits. The
    `account_bank` arg is Flutterwave's bank code (e.g. "044" for Access),
    not our internal Bank UUID - the caller is responsible for the lookup.
    """
    iso = _iso_country(country)

    if not settings.flw_secret_key:
        # Dev-fallback: synthesise a plausible-looking subaccount id so the
        # rest of the flow can be exercised without prod credentials.
        fake_id = f"RS_DEV_{abs(hash((account_number, business_name))) % 10**8:08d}"
        return {
            "subaccount_id": fake_id,
            "id": 0,
            "account_number": account_number,
            "account_bank": account_bank,
            "business_name": business_name,
            "split_value": split_value,
            "split_type": split_type,
            "country": iso,
            "_dev_fallback": True,
        }

    payload = {
        "account_bank": account_bank,
        "account_number": account_number,
        "business_name": business_name,
        "business_email": business_email,
        "business_mobile": business_mobile,
        "country": iso,
        "split_value": split_value,
        "split_type": split_type,
    }
    try:
        return await _flw_post("/v3/subaccounts", payload)
    except FlutterwaveError as e:
        # Idempotency: if FLW says a subaccount with this (bank, account)
        # combo already exists, reconcile to it rather than 502'ing the
        # admin. Their `subaccount_id` is stable so this is safe.
        msg = (e.body.get("message") if isinstance(e.body, dict) else str(e.body)) or ""
        if "already exists" in msg.lower():
            existing = await find_subaccount_by_account(account_bank, account_number)
            if existing:
                return existing
        raise


async def get_subaccount(subaccount_id: str) -> Dict[str, Any]:
    """Fetch a subaccount by ID. Used to surface status / balance in admin."""
    if not settings.flw_secret_key:
        return {"subaccount_id": subaccount_id, "balance": 0, "_dev_fallback": True}
    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(f"https://api.flutterwave.com/v3/subaccounts/{subaccount_id}")
        resp.raise_for_status()
        return resp.json().get("data") or {}


async def list_subaccounts() -> list:
    """Return every subaccount on the Flutterwave merchant account.

    Used to reconcile "subaccount already exists" errors - we re-fetch
    and pick the one matching our (account_bank, account_number) so we
    don't end up with orphaned records on FLW we can't reference.
    """
    if not settings.flw_secret_key:
        return []
    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get("https://api.flutterwave.com/v3/subaccounts")
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if isinstance(data, list):
            return data
        # Some FLW responses wrap it; defensive.
        return data.get("data", []) if isinstance(data, dict) else []


async def find_subaccount_by_account(account_bank: str, account_number: str) -> Optional[Dict[str, Any]]:
    """Look up an existing subaccount by its bank + account number tuple."""
    for sub in await list_subaccounts():
        if sub.get("account_number") == account_number and str(sub.get("account_bank") or "") == str(account_bank):
            return sub
    return None


async def transfer_to_bank(
    *,
    account_bank: str,
    account_number: str,
    amount: float,
    currency: str,
    narration: str,
    reference: str,
    beneficiary_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Disburse funds from the Jaratrade Flutterwave wallet directly to a bank
    account. Used by the manual seller-payout flow as the fallback when split
    settlement isn't sufficient (e.g. cross-border / GBP payouts to importers'
    refund destination).

    Returns Flutterwave's `data` block from POST /v3/transfers.
    """
    if not settings.flw_secret_key:
        return {
            "id": abs(hash(reference)) % 10**9,
            "reference": reference,
            "status": "NEW",
            "amount": amount,
            "currency": currency,
            "_dev_fallback": True,
        }
    payload: Dict[str, Any] = {
        "account_bank": account_bank,
        "account_number": account_number,
        "amount": float(amount),
        "currency": currency,
        "narration": narration[:80],
        "reference": reference,
    }
    if beneficiary_name:
        payload["beneficiary_name"] = beneficiary_name
    return await _flw_post("/v3/transfers", payload)


async def refund_payment(*, flw_transaction_id: str, amount: Optional[float] = None) -> Dict[str, Any]:
    """Issue a (full or partial) refund on a previously-successful charge.

    `flw_transaction_id` is Flutterwave's numeric ID (returned in the verify
    response under `data.id`), not our `tx_ref`. Pass `amount=None` for full
    refund.
    """
    if not settings.flw_secret_key:
        return {"status": "completed", "amount": amount, "_dev_fallback": True}

    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    body: Dict[str, Any] = {}
    if amount is not None:
        body["amount"] = float(amount)
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.post(
            f"https://api.flutterwave.com/v3/transactions/{flw_transaction_id}/refund",
            json=body,
        )
        resp.raise_for_status()
        return resp.json().get("data") or {}
