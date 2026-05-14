"""Password hashing, JWT encode/decode, and ID helpers."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import uuid4

import bcrypt
from jose import JWTError, jwt

from .config import get_settings

settings = get_settings()


# ─────────────────────────── Passwords ───────────────────────────
# Using bcrypt directly (passlib's bcrypt backend has known compat issues
# with bcrypt 5.x - direct usage is more robust).

def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ─────────────────────────── JWT ───────────────────────────

def create_access_token(*, subject: str, role: str, extra: Optional[dict] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_ttl_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Tuple[str, str]:
    """Returns (subject, role) or raises ValueError."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}") from e
    sub = payload.get("sub")
    role = payload.get("role")
    if not sub or not role:
        raise ValueError("Token missing subject or role")
    return sub, role


# ─────────────────────────── ID helpers ───────────────────────────
# We use UUID4 for primary keys exposed to clients.
# Frontend treats them as opaque strings - works identically to the legacy
# Fernet tokens. If you want truly Fernet-compatible IDs (long base64 strings
# that can be decrypted server-side), wire `cryptography.fernet.Fernet` here.

def new_id() -> str:
    return uuid4().hex


def secure_token(byte_length: int = 32) -> str:
    return secrets.token_urlsafe(byte_length)
