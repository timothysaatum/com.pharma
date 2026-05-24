"""
Organization Onboarding Schemas
Pydantic models for organization onboarding requests and responses
Uses existing organization schemas from app.schemas.organization
"""
from pydantic import EmailStr, Field, field_validator
from typing import Optional, Dict, Any, List
from datetime import datetime
import uuid

from app.schemas.base_schemas import BaseSchema
from app.schemas.branch_schemas import BranchCreate, BranchResponse
from app.schemas.organization import OrganizationResponse
from app.schemas.user_schema import UserResponse, UserCreate


class OrganizationOnboardingRequest(BaseSchema):
    """Complete request for onboarding a new organization"""
    
    # Organization details (matching your existing Organization model)
    name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        description="Organization name"
    )
    type: str = Field(
        ...,
        pattern="^(otc|pharmacy|hospital_pharmacy|chain)$",
        description="Type of organization (must match: otc, pharmacy, hospital_pharmacy, chain)"
    )
    license_number: Optional[str] = Field(
        None,
        max_length=100,
        description="Business license number"
    )
    tax_id: Optional[str] = Field(
        None,
        max_length=50,
        description="Tax identification number"
    )
    phone: Optional[str] = Field(
        None,
        max_length=20,
        description="Organization phone number"
    )
    email: Optional[EmailStr] = Field(
        None,
        description="Organization email address"
    )
    address: Optional[Dict[str, Any]] = Field(
        None,
        description="Organization address {street, city, state, zip, country}"
    )
    subscription_tier: str = Field(
        default="basic",
        pattern="^(basic|professional|enterprise)$",
        description="Subscription tier"
    )
    
    # Additional settings for organization
    currency: str = Field(
        default="GHS",
        max_length=3,
        description="Currency code (ISO 4217)"
    )
    timezone: str = Field(
        default="UTC",
        max_length=50,
        description="Timezone (e.g., Africa/Accra)"
    )
    additional_settings: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional organization settings"
    )
    
    # Admin user details
    admin: UserCreate = Field(..., description="Admin user details")
    
    # Branches to create
    branches: Optional[List[BranchCreate]] = Field(
        None,
        max_length=10,
        description="Branches to create (optional, max 10). If not provided, a default branch will be created."
    )
    
    @field_validator('name')
    @classmethod
    def validate_organization_name(cls, v: str) -> str:
        """Validate organization name"""
        if len(v.strip()) < 2:
            raise ValueError("Organization name must be at least 2 characters")
        
        # Check for invalid characters
        invalid_chars = ['<', '>', '"', "'", '\\', '/']
        if any(char in v for char in invalid_chars):
            raise ValueError("Organization name contains invalid characters")
        
        return v.strip()
    
    @field_validator('branches')
    @classmethod
    def validate_branches(cls, v: Optional[List[BranchCreate]]) -> Optional[List[BranchCreate]]:
        """Validate branches list"""
        if v and len(v) > 10:
            raise ValueError("Cannot create more than 10 branches during onboarding")
        
        # Check for duplicate branch names
        if v:
            names = [branch.name for branch in v]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate branch names detected")
        
        return v
    
    @field_validator('currency')
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Validate currency code"""
        # Common currency codes
        valid_currencies = [
            'USD', 'EUR', 'GBP', 'GHS', 'NGN', 'KES', 'ZAR', 'EGP',
            'JPY', 'CNY', 'INR', 'CAD', 'AUD', 'CHF', 'SEK', 'NOK', 'GHS'
        ]
        
        if v.upper() not in valid_currencies:
            raise ValueError(
                f"Currency code '{v}' not supported. "
                f"Supported currencies: {', '.join(valid_currencies)}"
            )
        
        return v.upper()
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "HealthCare Plus Pharmacy",
                "type": "pharmacy",
                "license_number": "PHR-2026-001",
                "tax_id": "TAX123456789",
                "phone": "+233501234567",
                "email": "info@healthcareplus.com",
                "address": {
                    "street": "123 Independence Avenue",
                    "city": "Accra",
                    "state": "Greater Accra",
                    "zip": "00233",
                    "country": "Ghana"
                },
                "subscription_tier": "professional",
                "currency": "GHS",
                "timezone": "Africa/Accra",
                "admin": {
                    "username": "laso",
                    "email": "admin@healthcareplus.com",
                    "full_name": "John Doe",
                    "password": "SecurePass!123",
                    "phone": "+233501234567",
                    "employee_id": "EMP-ADMIN-001"
                },
                "branches": [
                    {
                        "name": "Main Branch - Accra Central",
                        "phone": "+233501234567",
                        "email": "accra@healthcareplus.com",
                        "address": {
                            "street": "123 Independence Avenue",
                            "city": "Accra",
                            "state": "Greater Accra",
                            "zip": "00233",
                            "country": "Ghana"
                        }
                    },
                    {
                        "name": "East Legon Branch",
                        "phone": "+233501234568",
                        "email": "eastlegon@healthcareplus.com",
                        "address": {
                            "street": "45 East Legon Road",
                            "city": "Accra",
                            "state": "Greater Accra",
                            "zip": "00233",
                            "country": "Ghana"
                        }
                    }
                ]
            }
        }
    }


class OrganizationOnboardingResponse(BaseSchema):
    """Response after successful organization onboarding"""
    organization: OrganizationResponse
    admin_user: UserResponse
    branches: List[BranchResponse] = Field(..., description="Created branches")
    message: str
    
    # Access credentials
    temp_credentials: Optional[Dict[str, str]] = Field(
        None,
        description="Temporary access credentials (only returned on creation)"
    )


class SubscriptionUpdateRequest(BaseSchema):
    """Request to update organization subscription"""
    subscription_tier: str = Field(
        ...,
        pattern="^(basic|professional|enterprise)$",
        description="New subscription tier"
    )
    extend_months: int = Field(
        default=12,
        ge=1,
        le=60,
        description="Number of months to extend subscription (1-60)"
    )
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "subscription_tier": "professional",
                "extend_months": 12
            }
        }
    }


class OrganizationActivationRequest(BaseSchema):
    """Request to activate/deactivate organization"""
    reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Reason for activation/deactivation"
    )


class OrganizationListResponse(BaseSchema):
    """Paginated list of organizations"""
    items: list[OrganizationResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
    has_next: bool
    has_prev: bool


class OrganizationStatsResponse(BaseSchema):
    """Organization statistics"""
    organization_id: uuid.UUID
    total_branches: int = Field(..., ge=0)
    total_users: int = Field(..., ge=0)
    total_drugs: int = Field(..., ge=0)
    total_customers: int = Field(..., ge=0)
    total_sales_today: int = Field(..., ge=0)
    total_sales_this_month: int = Field(..., ge=0)
    subscription_status: str
    subscription_expires_at: Optional[datetime] = None
    days_until_expiry: Optional[int] = None
    is_active: bool


class OrganizationSettingsUpdate(BaseSchema):
    """Update organization settings"""
    currency: Optional[str] = Field(None, max_length=3)
    timezone: Optional[str] = Field(None, max_length=50)
    date_format: Optional[str] = Field(None, max_length=20)
    time_format: Optional[str] = Field(None, pattern="^(12h|24h)$")
    tax_inclusive: Optional[bool] = None
    low_stock_threshold: Optional[int] = Field(None, ge=0, le=1000)
    enable_loyalty_program: Optional[bool] = None
    enable_prescriptions: Optional[bool] = None
    enable_batch_tracking: Optional[bool] = None
    auto_generate_sku: Optional[bool] = None
    receipt_footer: Optional[str] = Field(None, max_length=500)
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "currency": "GHS",
                "timezone": "Africa/Accra",
                "tax_inclusive": False,
                "low_stock_threshold": 20,
                "enable_loyalty_program": True,
                "receipt_footer": "Thank you for your business!"
            }
        }
    }