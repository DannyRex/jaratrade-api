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


def build_inline_config(
    *,
    tx_ref: str,
    amount: float,
    currency: str,
    customer: Dict[str, str],
    order_id: str,
    commission_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the Flutterwave Inline-checkout config.

    ``commission_rate`` is the decimal split (e.g. 0.02 for 2%) routed to the
    platform's Flutterwave subaccount. Pass ``None`` to fall back to the
    historic 2% default; new callers should look the rate up via
    ``settings_router.read_commission_rate(db)`` and pass it in so the
    admin's configured value drives the split.
    """
    split = []
    if settings.flw_commission_subaccount_id:
        rate = 0.02 if commission_rate is None else commission_rate
        split.append({
            "id": settings.flw_commission_subaccount_id,
            "transaction_charge_type": "percentage",
            "transaction_charge": f"{rate:.4f}",
        })
    return {
        "public_key": settings.flw_public_key,
        "tx_ref": tx_ref,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "payment_options": "card,banktransfer,ussd",
        "customer": customer,
        "customizations": {
            "title": "Jaratrade",
            "description": f"Payment for order {order_id}",
            "logo": False,
        },
        "split": split,
    }


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
