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


def build_inline_config(*, tx_ref: str, amount: float, currency: str, customer: Dict[str, str], order_id: str) -> Dict[str, Any]:
    split = []
    if settings.flw_commission_subaccount_id:
        split.append({
            "id": settings.flw_commission_subaccount_id,
            "transaction_charge_type": "percentage",
            "transaction_charge": "0.02",
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
    """Look up a transaction by tx_ref. Returns Flutterwave's data block."""
    if not settings.flw_secret_key:
        # Dev fallback: pretend it succeeded so the full flow can be exercised locally.
        return {"status": "successful", "tx_ref": tx_ref, "amount": 0, "currency": "NGN", "_dev_fallback": True}

    headers = {"Authorization": f"Bearer {settings.flw_secret_key}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(
            "https://api.flutterwave.com/v3/transactions/verify_by_reference",
            params={"tx_ref": tx_ref},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("data") or {}
