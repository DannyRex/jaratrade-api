"""Common FastAPI dependencies."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, status
from sqlalchemy.orm import Session

from .database import get_db
from .envelope import fail
from .models.user import User
from .security import decode_token


def get_bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise fail("Authentication required", code=status.HTTP_401_UNAUTHORIZED)
    return authorization.split(" ", 1)[1].strip()


def get_current_user(
    token: str = Depends(get_bearer_token),
    db: Session = Depends(get_db),
) -> User:
    try:
        sub, _role = decode_token(token)
    except ValueError as e:
        raise fail("Invalid or expired token", code=status.HTTP_401_UNAUTHORIZED) from e
    user = db.get(User, sub)
    if not user:
        raise fail("User not found", code=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        raise fail("Account is not active", code=status.HTTP_403_FORBIDDEN)
    return user


def require_role(role: str):
    """Factory: returns a dependency that asserts the JWT role matches."""

    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role != role:
            raise fail(f"Requires {role} role", code=status.HTTP_403_FORBIDDEN)
        return user

    return _checker


require_importer = require_role("importer")
require_exporter = require_role("exporter")
require_admin = require_role("admin")
