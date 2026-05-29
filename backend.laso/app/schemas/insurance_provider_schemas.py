"""
Insurance Provider Schemas

Provides validation and serialization for insurance provider management.
Supports CRUD operations for staff managing insurance partnerships.
"""

from pydantic import Field, field_validator, model_validator, ConfigDict
from typing import Optional, Dict, Any
from datetime import datetime
import uuid

from app.schemas.base_schemas import BaseSchema, TimestampSchema, SyncSchema


class InsuranceProviderAddress(BaseSchema):
    """Address information for insurance provider"""
    street: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)


class InsuranceProviderBase(BaseSchema):
    """Base insurance provider fields"""
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Insurance provider name"
    )
    
    code: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Unique code for insurance provider"
    )
    
    logo_url: Optional[str] = Field(
        None,
        max_length=500,
        description="URL to provider logo"
    )
    
    phone: Optional[str] = Field(
        None,
        max_length=20,
        description="Provider contact phone"
    )
    
    email: Optional[str] = Field(
        None,
        max_length=255,
        description="Provider contact email"
    )
    
    website: Optional[str] = Field(
        None,
        max_length=500,
        description="Provider website URL"
    )
    
    address: Optional[InsuranceProviderAddress] = Field(
        None,
        description="Provider office address"
    )
    
    primary_contact_name: Optional[str] = Field(
        None,
        max_length=255,
        description="Primary contact person name"
    )
    
    primary_contact_phone: Optional[str] = Field(
        None,
        max_length=20,
        description="Primary contact phone"
    )
    
    primary_contact_email: Optional[str] = Field(
        None,
        max_length=255,
        description="Primary contact email"
    )
    
    # ============================================
    # OPERATIONAL SETTINGS
    # ============================================
    
    billing_cycle: str = Field(
        default="monthly",
        pattern="^(daily|weekly|monthly|quarterly|annually)$",
        description="Billing cycle frequency"
    )
    
    payment_terms: str = Field(
        default="NET30",
        pattern="^(NET15|NET30|NET60|COD|PREPAID)$",
        description="Payment terms code"
    )
    
    requires_card_verification: bool = Field(
        default=False,
        description="Whether card verification is required"
    )
    
    requires_preauth: bool = Field(
        default=False,
        description="Whether pre-authorization is required"
    )
    
    verification_endpoint: Optional[str] = Field(
        None,
        max_length=500,
        description="API endpoint for card verification"
    )
    
    is_active: bool = Field(
        default=True,
        description="Whether provider is active"
    )
    
    notes: Optional[str] = Field(
        None,
        max_length=1000,
        description="Internal notes about provider"
    )
    
    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        """Validate that code is alphanumeric with underscores"""
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError("Code must be alphanumeric with underscores only")
        return v.upper()


class InsuranceProviderCreate(InsuranceProviderBase):
    """Schema for creating insurance provider"""
    pass


class InsuranceProviderUpdate(BaseSchema):
    """Schema for updating insurance provider"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    logo_url: Optional[str] = Field(None, max_length=500)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    address: Optional[InsuranceProviderAddress] = None
    primary_contact_name: Optional[str] = Field(None, max_length=255)
    primary_contact_phone: Optional[str] = Field(None, max_length=20)
    primary_contact_email: Optional[str] = Field(None, max_length=255)
    billing_cycle: Optional[str] = Field(
        None,
        pattern="^(daily|weekly|monthly|quarterly|annually)$"
    )
    payment_terms: Optional[str] = Field(
        None,
        pattern="^(NET15|NET30|NET60|COD|PREPAID)$"
    )
    requires_card_verification: Optional[bool] = None
    requires_preauth: Optional[bool] = None
    verification_endpoint: Optional[str] = Field(None, max_length=500)
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=1000)


class InsuranceProviderResponse(InsuranceProviderBase, TimestampSchema, SyncSchema):
    """Schema for insurance provider API responses"""
    id: uuid.UUID = Field(..., description="Provider UUID")
    organization_id: uuid.UUID = Field(..., description="Organization UUID")
    
    model_config = ConfigDict(from_attributes=True)


class InsuranceProviderListResponse(BaseSchema):
    """Schema for list of insurance providers"""
    providers: list[InsuranceProviderResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)


class InsuranceProviderSearchItem(BaseSchema):
    """Simplified insurance provider for search/select"""
    id: uuid.UUID
    name: str
    code: str
    logo_url: Optional[str] = None
    is_active: bool
