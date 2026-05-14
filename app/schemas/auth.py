"""Auth-related response shapes (mirroring the legacy contract)."""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict


class BusinessDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    business_name: Optional[str] = None
    business_email: Optional[str] = None
    business_address: Optional[str] = None
    business_reg_number: Optional[str] = None
    business_type: Optional[str] = None
    business_country: Optional[str] = None
    annual_turnover: Optional[Any] = None
    duration_in_business: Optional[int] = None
    documents: Optional[str] = None
    tin: Optional[str] = None
    valid_identification: Optional[str] = None


class UserDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    firstname: Optional[str] = None
    middlename: Optional[str] = None
    lastname: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    business: BusinessDTO = BusinessDTO()
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    dob: Optional[str] = None
    fav_product: List[str] = []
    product_delivered: int = 0
    profile_name: Optional[str] = None
    review_count: int = 0
    status: int = 1


class LoginPayload(UserDTO):
    token: str
