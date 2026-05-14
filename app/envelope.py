"""Response envelope + standard error helper.

The legacy API wraps every successful response as
``{ status: bool, message: str, payload: <data> }``. Errors include an
``errors`` list. We mirror that contract here so the existing frontend works
without modification.
"""
from __future__ import annotations

from typing import Any, Generic, List, Optional, TypeVar

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    status: bool = True
    message: str = "success"
    payload: T


class ErrorEnvelope(BaseModel):
    status: bool = False
    message: str = "Request failed"
    errors: List[str] = []


def success(payload: Any = None, message: str = "success") -> dict:
    return {"status": True, "message": message, "payload": payload}


def fail(message: str, errors: Optional[List[str]] = None, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"status": False, "message": message, "errors": errors or [message]},
    )


class Paged(BaseModel, Generic[T]):
    """Generic paged-rows envelope ({rows, total_length, page, len})."""
    rows: List[T]
    total_length: int
    page: int
    len: int


class DataPaged(BaseModel, Generic[T]):
    """Generic data+meta envelope ({data, meta: {paging: {total, page, len}}})."""
    data: List[T]
    meta: dict
