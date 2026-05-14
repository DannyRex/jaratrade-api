"""Cloudinary upload helper.

Uses Cloudinary's REST upload API directly (no SDK dependency). When credentials
aren't configured, falls back to base64 data URLs so dev environments still work.
"""
from __future__ import annotations

import base64
import hashlib
import time
from typing import Optional

import httpx

from ..config import get_settings

settings = get_settings()

UPLOAD_URL = "https://api.cloudinary.com/v1_1/{cloud}/auto/upload"


def _signature(params: dict) -> str:
    body = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v not in (None, ""))
    return hashlib.sha1((body + settings.cloudinary_api_secret).encode("utf-8")).hexdigest()


async def upload_file(content: bytes, filename: str, folder: str = "products") -> str:
    """Upload bytes to Cloudinary or return a base64 data URL fallback."""
    if not settings.cloudinary_cloud_name or not settings.cloudinary_api_key or not settings.cloudinary_api_secret:
        # Dev fallback: emit a data URL so the API still functions without secrets
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "pdf": "application/pdf"}.get(
            suffix, "application/octet-stream"
        )
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"

    timestamp = int(time.time())
    sign_params = {"folder": folder, "timestamp": timestamp}
    signature = _signature(sign_params)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            UPLOAD_URL.format(cloud=settings.cloudinary_cloud_name),
            data={
                "api_key": settings.cloudinary_api_key,
                "timestamp": timestamp,
                "folder": folder,
                "signature": signature,
            },
            files={"file": (filename, content)},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("secure_url") or body.get("url") or ""
