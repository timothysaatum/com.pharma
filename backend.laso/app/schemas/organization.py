from app.schemas.base_schemas import BaseSchema, SyncSchema, TimestampSchema
from pydantic import (
    EmailStr, Field, field_validator, 
    ConfigDict
)
from typing import Optional, Dict, Any
from datetime import datetime
import uuid
import re


class OrganizationBase(BaseSchema):
    name: str = Field(..., min_length=2, max_length=255, description="Organization name")
    type: str = Field(
        ..., 
        pattern="^(otc|pharmacy|hospital_pharmacy|chain)$",
        description="Organization type"
    )
    license_number: Optional[str] = Field(None, max_length=100)
    tax_id: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20, pattern=r'^\+?[\d\s\-\(\)]+$')
    email: Optional[EmailStr] = None
    address: Optional[Dict[str, Any]] = Field(
        None,
        description="Address as JSON: {street, city, state, zip, country}"
    )
    settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Organization settings and preferences"
    )

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        """Validate and normalize phone number"""
        if v:
            cleaned = re.sub(r'[^\d+]', '', v)
            if len(cleaned) < 10:
                raise ValueError("Phone number must have at least 10 digits")
            return cleaned
        return v


class OrganizationCreate(OrganizationBase):
    subscription_tier: str = Field(
        default="basic",
        pattern="^(basic|professional|enterprise)$"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "MediCare Pharmacies Ltd",
                "type": "pharmacy",
                "license_number": "PHR-2024-001",
                "tax_id": "TAX123456",
                "phone": "+1234567890",
                "email": "info@medicare.com",
                "address": {
                    "street": "123 Main St",
                    "city": "New York",
                    "state": "NY",
                    "zip": "10001",
                    "country": "USA"
                },
                "subscription_tier": "professional"
            }
        }
    )


class OrganizationUpdate(BaseSchema):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    type: Optional[str] = Field(None, pattern="^(otc|pharmacy|hospital_pharmacy|chain)$")
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[Dict[str, Any]] = None
    settings: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class OrganizationResponse(OrganizationBase, TimestampSchema, SyncSchema):
    id: uuid.UUID
    is_active: bool
    subscription_tier: str
    subscription_expires_at: Optional[datetime] = None

    @field_validator('settings')
    @classmethod
    def sanitize_settings(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Remove sensitive keys from settings"""
        sensitive_keys = ['api_keys', 'secrets', 'credentials']
        return {k: v for k, v in v.items() if k not in sensitive_keys}