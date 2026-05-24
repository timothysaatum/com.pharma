"""
Customer Schemas
Updated to support price contracts, insurance integration, and robust validation
"""
from app.schemas.base_schemas import BaseSchema, SyncSchema, TimestampSchema
from pydantic import (
    EmailStr, Field, ConfigDict, field_validator, model_validator
)
from typing import Optional, Dict, Any, List
from datetime import datetime, date
import uuid


# ============================================
# Customer Base Schemas
# ============================================

class CustomerBase(BaseSchema):
    """Base customer"""
    customer_type: str = Field(
        default="walk_in",
        pattern="^(walk_in|registered|insurance|corporate)$",
        description="Customer classification"
    )
    
    first_name: Optional[str] = Field(
        None, 
        min_length=1,
        max_length=255,
        description="Customer first name"
    )
    
    last_name: Optional[str] = Field(
        None,
        min_length=1, 
        max_length=255,
        description="Customer last name"
    )
    
    phone: Optional[str] = Field(
        None,
        min_length=10,
        max_length=20,
        pattern=r'^\+?[0-9\-\s()]+$',
        description="Customer phone number with country code"
    )
    
    email: Optional[EmailStr] = Field(
        None,
        description="Customer email address"
    )
    
    date_of_birth: Optional[date] = Field(
        None,
        description="Customer date of birth (for age verification)"
    )
    
    address: Optional[Dict[str, Any]] = Field(
        None,
        description="Customer address: {street, city, state, zip, country}"
    )
    
    # ============================================
    # INSURANCE FIELDS
    # ============================================
    
    insurance_provider_id: Optional[uuid.UUID] = Field(
        None,
        description="Link to InsuranceProvider for contract eligibility"
    )
    
    insurance_member_id: Optional[str] = Field(
        None,
        max_length=100,
        description="Member/policy ID with insurance company"
    )
    
    insurance_card_image_url: Optional[str] = Field(
        None,
        description="URL to scanned insurance card for verification"
    )
    
    # ============================================
    # PRICING CONTRACT PREFERENCE
    # ============================================
    
    preferred_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Customer's preferred pricing contract (for corporate/staff)"
    )
    
    # ============================================
    # CONTACT PREFERENCES
    # ============================================
    
    preferred_contact_method: str = Field(
        default="email",
        pattern="^(email|phone|sms)$",
        description="Preferred method of contact"
    )
    
    marketing_consent: bool = Field(
        default=False,
        description="Customer consent for marketing communications"
    )
    
    # ============================================
    # VALIDATORS
    # ============================================
    
    @field_validator('date_of_birth')
    @classmethod
    def validate_dob(cls, v: Optional[date]) -> Optional[date]:
        """Validate date of birth is not in the future and reasonable"""
        if v is None:
            return v
        
        today = date.today()
        if v > today:
            raise ValueError("Date of birth cannot be in the future")
        
        # Check age is reasonable (not more than 150 years old)
        age = (today - v).days // 365
        if age > 150:
            raise ValueError("Date of birth is too far in the past")
        
        return v
    
    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        """Clean and validate phone number"""
        if v is None:
            return v
        
        # Remove common separators for storage
        cleaned = v.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        
        # Must have at least 10 digits
        digits = ''.join(c for c in cleaned if c.isdigit())
        if len(digits) < 10:
            raise ValueError("Phone number must contain at least 10 digits")
        
        return v  # Return original format for display


class CustomerCreate(CustomerBase):
    """Schema for creating a new customer"""
    organization_id: uuid.UUID = Field(
        ...,
        description="Organization this customer belongs to"
    )
    
    @model_validator(mode='after')
    def validate_registered_customer(self) -> 'CustomerCreate':
        """Validate required fields for registered customers"""
        if self.customer_type == 'registered':
            if not self.first_name or not self.last_name:
                raise ValueError("First name and last name required for registered customers")
            
            if not self.phone and not self.email:
                raise ValueError("At least phone or email required for registered customers")
        
        return self
    
    @model_validator(mode='after')
    def validate_insurance_customer(self) -> 'CustomerCreate':
        """Validate insurance customer requirements"""
        if self.customer_type == 'insurance':
            if not self.insurance_provider_id:
                raise ValueError("Insurance provider required for insurance customers")
            
            if not self.insurance_member_id:
                raise ValueError("Insurance member ID required for insurance customers")
            
            if not self.first_name or not self.last_name:
                raise ValueError("Full name required for insurance customers")
        
        return self
    
    @model_validator(mode='after')
    def validate_corporate_customer(self) -> 'CustomerCreate':
        """Validate corporate customer requirements"""
        if self.customer_type == 'corporate':
            if not self.preferred_contract_id:
                raise ValueError("Preferred contract required for corporate customers")
            
            if not self.first_name or not self.last_name:
                raise ValueError("Full name required for corporate customers")
        
        return self


class CustomerUpdate(BaseSchema):
    """Schema for updating customer information (partial updates allowed)"""
    first_name: Optional[str] = Field(None, min_length=1, max_length=255)
    last_name: Optional[str] = Field(None, min_length=1, max_length=255)
    phone: Optional[str] = Field(None, min_length=10, max_length=20)
    email: Optional[EmailStr] = None
    date_of_birth: Optional[date] = None
    address: Optional[Dict[str, Any]] = None
    
    # Insurance updates
    insurance_provider_id: Optional[uuid.UUID] = None
    insurance_member_id: Optional[str] = Field(None, max_length=100)
    insurance_card_image_url: Optional[str] = None
    
    # Contract preference updates
    preferred_contract_id: Optional[uuid.UUID] = None
    
    # Contact preferences
    preferred_contact_method: Optional[str] = Field(
        None,
        pattern="^(email|phone|sms)$"
    )
    marketing_consent: Optional[bool] = None
    
    @field_validator('date_of_birth')
    @classmethod
    def validate_dob(cls, v: Optional[date]) -> Optional[date]:
        """Validate date of birth"""
        if v is None:
            return v
        
        today = date.today()
        if v > today:
            raise ValueError("Date of birth cannot be in the future")
        
        age = (today - v).days // 365
        if age > 150:
            raise ValueError("Date of birth is too far in the past")
        
        return v


class CustomerResponse(CustomerBase, TimestampSchema, SyncSchema):
    """Schema for customer API responses"""
    id: uuid.UUID
    organization_id: uuid.UUID
    
    # Loyalty program
    loyalty_points: int = Field(default=0, ge=0)
    loyalty_tier: str = Field(default='bronze')

    # Denormalized purchase counters (kept in sync by SalesService)
    total_orders: int = Field(default=0, ge=0)
    total_value: float = Field(default=0.0, ge=0)
    
    # Status
    is_active: bool = Field(default=True)
    deleted_at: Optional[datetime] = None
    
    # Security: Exclude sensitive health information from API responses
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class CustomerWithDetails(CustomerResponse):
    """Customer with relationship details"""
    
    # Insurance provider details (if applicable)
    insurance_provider_name: Optional[str] = Field(
        None,
        description="Name of insurance company"
    )
    insurance_provider_code: Optional[str] = Field(
        None,
        description="Insurance provider code"
    )
    
    # Preferred contract details (if applicable)
    preferred_contract_name: Optional[str] = Field(
        None,
        description="Name of preferred pricing contract"
    )
    preferred_contract_discount: Optional[float] = Field(
        None,
        description="Discount percentage from preferred contract"
    )
    
    # Purchase statistics — populated by CustomerService._build_with_details
    # via a live aggregate query on Sale. NOT aliased from the ORM model.
    total_purchases: int = Field(default=0, ge=0)
    total_spent: float = Field(default=0.0, ge=0)
    last_purchase_date: Optional[datetime] = None
    
    # Computed properties
    @property
    def full_name(self) -> Optional[str]:
        """Get customer's full name"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.last_name
    
    @property
    def has_insurance(self) -> bool:
        """Check if customer has insurance"""
        return self.insurance_provider_id is not None
    
    @property
    def age(self) -> Optional[int]:
        """Calculate customer's age"""
        if not self.date_of_birth:
            return None
        
        today = date.today()
        age = today.year - self.date_of_birth.year
        
        # Adjust if birthday hasn't occurred this year
        if (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day):
            age -= 1
        
        return age
    
    @property
    def is_senior_citizen(self) -> bool:
        """Check if customer qualifies for senior discount (60+)"""
        age = self.age
        return age >= 60 if age else False


# ============================================
# Customer Search & Filters
# ============================================

class CustomerFilters(BaseSchema):
    """Filters for searching customers"""
    customer_type: Optional[str] = Field(
        None,
        pattern="^(walk_in|registered|insurance|corporate)$"
    )
    
    loyalty_tier: Optional[str] = Field(
        None,
        pattern="^(bronze|silver|gold|platinum)$"
    )
    
    insurance_provider_id: Optional[uuid.UUID] = Field(
        None,
        description="Filter by insurance provider"
    )
    
    preferred_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Filter by preferred contract"
    )
    
    is_active: Optional[bool] = None
    
    search: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Search in name, phone, email, member ID"
    )
    
    min_loyalty_points: Optional[int] = Field(None, ge=0)
    
    # Sorting
    sort_by: Optional[str] = Field(
        default='created_at',
        pattern="^(created_at|first_name|last_name|loyalty_points|total_spent)$"
    )
    
    sort_order: Optional[str] = Field(
        default='desc',
        pattern="^(asc|desc)$"
    )


class CustomerListResponse(BaseSchema):
    """Response for customer list"""
    customers: List[CustomerResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)


# ============================================
# Customer Actions
# ============================================

class UpdateInsuranceRequest(BaseSchema):
    """Request to update customer insurance information"""
    insurance_provider_id: uuid.UUID = Field(
        ...,
        description="Insurance provider ID"
    )
    
    insurance_member_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Member/policy number"
    )
    
    insurance_card_image_url: Optional[str] = Field(
        None,
        description="URL to scanned insurance card"
    )
    
    verify_immediately: bool = Field(
        default=True,
        description="Whether to verify insurance immediately"
    )


class UpdateContractPreferenceRequest(BaseSchema):
    """Request to update customer's preferred contract"""
    preferred_contract_id: uuid.UUID = Field(
        ...,
        description="Preferred pricing contract ID"
    )
    
    notes: Optional[str] = Field(
        None,
        max_length=500,
        description="Notes about contract preference"
    )


class AwardLoyaltyPointsRequest(BaseSchema):
    """Request to manually award loyalty points"""
    points: int = Field(
        ...,
        gt=0,
        description="Number of points to award"
    )
    
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Reason for awarding points"
    )


class DeductLoyaltyPointsRequest(BaseSchema):
    """Request to deduct loyalty points"""
    points: int = Field(
        ...,
        gt=0,
        description="Number of points to deduct"
    )
    
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Reason for deduction"
    )


class RedeemLoyaltyPointsRequest(BaseSchema):
    """Request to redeem loyalty points for discount"""
    points_to_redeem: int = Field(
        ...,
        gt=0,
        description="Number of points to redeem"
    )
    
    sale_id: uuid.UUID = Field(
        ...,
        description="Sale to apply discount to"
    )


class CustomerStatistics(BaseSchema):
    """Customer purchase statistics"""
    customer_id: uuid.UUID
    customer_name: str
    customer_type: str
    
    # Purchase history
    total_purchases: int = Field(..., ge=0)
    total_spent: float = Field(..., ge=0)
    average_purchase: float = Field(..., ge=0)
    
    # Loyalty
    current_loyalty_points: int = Field(..., ge=0)
    loyalty_tier: str
    lifetime_points_earned: int = Field(..., ge=0)
    lifetime_points_redeemed: int = Field(..., ge=0)
    
    # Timing
    first_purchase_date: Optional[datetime]
    last_purchase_date: Optional[datetime]
    days_since_last_purchase: Optional[int]
    
    # Preferences
    preferred_contract_name: Optional[str]
    insurance_provider_name: Optional[str]
    
    # Top purchases
    top_drugs_purchased: List[dict] = Field(default_factory=list)
    
    # Date range for stats
    start_date: datetime
    end_date: datetime


# ============================================
# Customer Quick Lookup
# ============================================

class CustomerQuickLookup(BaseSchema):
    """Quick customer lookup by various identifiers"""
    id: uuid.UUID
    full_name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    customer_type: str
    loyalty_points: int
    
    # Quick contract info
    has_insurance: bool
    insurance_provider_name: Optional[str]
    preferred_contract_name: Optional[str]
    eligible_for_senior_discount: bool
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class CustomerSearchResult(BaseSchema):
    """Search result for customer lookup"""
    matches: List[CustomerQuickLookup]
    total: int
    search_term: str