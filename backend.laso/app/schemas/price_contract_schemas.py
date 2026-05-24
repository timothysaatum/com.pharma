"""
Price Contract Schemas

Features:
- Comprehensive validation rules
- Security controls
- Audit tracking
- Performance metrics
- Advanced filtering
"""
from pydantic import Field, model_validator, field_validator, ConfigDict
from typing import Optional, List, Dict, Any, Annotated
from datetime import datetime, date, timedelta
from decimal import Decimal
import uuid

from app.schemas.base_schemas import BaseSchema, TimestampSchema, SyncSchema


# ============================================
# Price Contract Base Schemas
# ============================================

class PriceContractBase(BaseSchema):
    """Base price contract fields with comprehensive validation"""
    
    contract_code: str = Field(
        ..., 
        min_length=2, 
        max_length=50,
        pattern=r'^[A-Z0-9\-]+$',
        description="Unique code: GLICO-STD, SIC-PREM, STAFF-20 (uppercase alphanumeric with hyphens)"
    )
    
    contract_name: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Display name: 'GLICO Insurance Standard Plan'"
    )
    
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Contract terms, conditions, and usage notes"
    )
    
    contract_type: str = Field(
        ...,
        pattern="^(insurance|corporate|staff|senior_citizen|standard|wholesale|promotional)$",
        description="Type of contract"
    )
    
    is_default_contract: bool = Field(
        default=False,
        description="Is this the default pricing contract?"
    )
    
    # ============================================
    # DISCOUNT CONFIGURATION
    # ============================================
    
    discount_type: str = Field(
        default='percentage',
        pattern="^(percentage|fixed_amount|tiered|custom)$",
        description="Type of discount calculation"
    )
    
    discount_percentage: Decimal = Field(
        default=Decimal('0.00'),
        ge=0.0,
        le=100.0,
        description="Discount percentage: 5.00, 10.00, 15.00, 20.00"
    )
    
    # ============================================
    # APPLICABILITY RULES
    # ============================================
    
    applies_to_prescription_only: bool = Field(
        default=False,
        description="If TRUE, contract only applies to prescription drugs"
    )
    
    applies_to_otc: bool = Field(
        default=True,
        description="If TRUE, contract applies to over-the-counter drugs"
    )
    
    excluded_drug_categories: List[uuid.UUID] = Field(
        default_factory=list,
        max_length=100,
        description="Drug categories excluded from contract"
    )
    
    excluded_drug_ids: List[uuid.UUID] = Field(
        default_factory=list,
        max_length=500,
        description="Specific drugs excluded from contract"
    )
    
    # ============================================
    # PRICE LIMITS & CAPS
    # ============================================
    
    minimum_price_override: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Never go below this price even with discount (floor price)"
    )

    maximum_discount_amount: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Cap the discount amount per item (maximum savings)"
    )

    minimum_purchase_amount: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Minimum purchase amount to qualify for contract"
    )

    maximum_purchase_amount: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Maximum purchase amount for contract applicability"
    )

    
    # ============================================
    # BRANCH APPLICABILITY
    # ============================================
    
    applies_to_all_branches: bool = Field(
        default=True,
        description="If FALSE, only specific branches can use this contract"
    )
    
    applicable_branch_ids: List[uuid.UUID] = Field(
        default_factory=list,
        max_length=100,
        description="Specific branches where contract is valid"
    )
    
    # ============================================
    # TIME VALIDITY
    # ============================================
    
    effective_from: date = Field(
        ...,
        description="Contract start date (inclusive)"
    )
    
    effective_to: Optional[date] = Field(
        None,
        description="Contract end date (inclusive). NULL = no expiry"
    )
    
    # ============================================
    # USAGE CONTROLS & SECURITY
    # ============================================
    
    requires_verification: bool = Field(
        default=False,
        description="Require verification (e.g., insurance card scan, ID check)"
    )
    
    requires_approval: bool = Field(
        default=False,
        description="Require manager approval during checkout"
    )
    
    allowed_user_roles: List[str] = Field(
        default_factory=list,
        max_length=10,
        description="User roles allowed to apply this contract. Empty = all roles"
    )
    
    daily_usage_limit: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum number of times contract can be used per day (fraud prevention)"
    )
    
    per_customer_usage_limit: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum times a single customer can use this contract"
    )
    
    # ============================================
    # INSURANCE-SPECIFIC FIELDS
    # ============================================
    
    insurance_provider_id: Optional[uuid.UUID] = Field(
        None,
        description="Link to insurance provider (required if contract_type = 'insurance')"
    )
    
    copay_amount: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Fixed copay amount patient must pay"
    )
    
    copay_percentage: Optional[Decimal] = Field(
        None,
        ge=0.0,
        le=100.0,
        description="Percentage of price patient must pay as copay"
    )
    
    requires_preauthorization: bool = Field(
        default=False,
        description="Requires pre-authorization from insurance company"
    )
    
    # ============================================
    # STATUS
    # ============================================
    
    status: str = Field(
        default='draft',
        pattern="^(draft|active|suspended|expired|cancelled)$",
        description="Contract status"
    )
    
    is_active: bool = Field(
        default=True,
        description="Is contract currently active"
    )
    
    # ============================================
    # VALIDATORS
    # ============================================
    
    @field_validator('contract_code')
    @classmethod
    def validate_contract_code(cls, v: str) -> str:
        """Validate and normalize contract code"""
        # Convert to uppercase
        v = v.upper().strip()
        
        # Check for valid characters
        if not all(c.isalnum() or c == '-' for c in v):
            raise ValueError("Contract code can only contain letters, numbers, and hyphens")
        
        # Cannot start or end with hyphen
        if v.startswith('-') or v.endswith('-'):
            raise ValueError("Contract code cannot start or end with a hyphen")
        
        # No consecutive hyphens
        if '--' in v:
            raise ValueError("Contract code cannot contain consecutive hyphens")
        
        return v
    
    @field_validator('allowed_user_roles')
    @classmethod
    def validate_roles(cls, v: List[str]) -> List[str]:
        """Validate user roles"""
        valid_roles = {'super_admin', 'admin', 'manager', 'pharmacist', 'cashier', 'viewer'}
        
        for role in v:
            if role not in valid_roles:
                raise ValueError(f"Invalid role: {role}. Must be one of {valid_roles}")
        
        return list(set(v))  # Remove duplicates


class PriceContractCreate(PriceContractBase):
    """Schema for creating a new price contract with comprehensive validation"""
    
    organization_id: uuid.UUID = Field(
        ...,
        description="Organization this contract belongs to"
    )
    
    @model_validator(mode='after')
    def validate_contract_logic(self) -> 'PriceContractCreate':
        """Validate business logic and cross-field constraints"""
        
        # 1. Validate date range
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        
        # 2. Validate insurance contracts
        if self.contract_type == 'insurance':
            if not self.insurance_provider_id:
                raise ValueError("insurance_provider_id required for insurance contracts")
            
            if not self.copay_amount and not self.copay_percentage:
                raise ValueError("Either copay_amount or copay_percentage required for insurance contracts")
            
            if self.copay_amount and self.copay_percentage:
                raise ValueError("Cannot specify both copay_amount and copay_percentage")
        
        # 3. Validate branch applicability
        if not self.applies_to_all_branches and len(self.applicable_branch_ids) == 0:
            raise ValueError("applicable_branch_ids required when applies_to_all_branches is False")
        
        if self.applies_to_all_branches and len(self.applicable_branch_ids) > 0:
            raise ValueError("applicable_branch_ids should be empty when applies_to_all_branches is True")
        
        # 4. Validate default contract
        if self.is_default_contract:
            if self.contract_type != 'standard':
                raise ValueError("Only 'standard' contracts can be default")
            
            if self.discount_percentage != Decimal('0.00'):
                raise ValueError("Default contract should have 0% discount")
            
            if not self.applies_to_all_branches:
                raise ValueError("Default contract must apply to all branches")
        
        # 5. Validate discount and drug applicability
        if not self.applies_to_prescription_only and not self.applies_to_otc:
            raise ValueError("Contract must apply to at least one drug type (prescription or OTC)")
        
        # 6. Validate price limits
        if self.minimum_purchase_amount and self.maximum_purchase_amount:
            if self.minimum_purchase_amount > self.maximum_purchase_amount:
                raise ValueError("minimum_purchase_amount cannot exceed maximum_purchase_amount")
        
        # 7. Validate promotional contracts
        if self.contract_type == 'promotional':
            if not self.effective_to:
                raise ValueError("Promotional contracts must have an expiry date")
            
            # Promotional contracts should not be more than 1 year
            if self.effective_to:
                days_duration = (self.effective_to - self.effective_from).days
                if days_duration > 365:
                    raise ValueError("Promotional contracts cannot exceed 365 days")
        
        # 8. Validate wholesale contracts
        if self.contract_type == 'wholesale':
            if not self.minimum_purchase_amount:
                raise ValueError("Wholesale contracts must specify minimum_purchase_amount")
            
            if self.discount_percentage > Decimal('30.00'):
                raise ValueError("Wholesale discount cannot exceed 30% without special approval")
        
        # 9. Validate senior citizen contracts
        if self.contract_type == 'senior_citizen':
            if self.discount_percentage > Decimal('15.00'):
                raise ValueError("Senior citizen discount typically should not exceed 15%")
            
            if self.requires_verification:
                # This is actually good - should verify age
                pass
        
        # 10. Validate staff contracts
        if self.contract_type == 'staff':
            if not self.allowed_user_roles:
                raise ValueError("Staff contracts should restrict which roles can apply them")
            
            if 'manager' not in self.allowed_user_roles and 'admin' not in self.allowed_user_roles:
                raise ValueError("Staff contracts should require manager or admin approval")
        
        return self


class PriceContractUpdate(BaseSchema):
    """Schema for updating a price contract (partial updates allowed)"""
    
    contract_name: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    
    discount_percentage: Optional[Decimal] = Field(
        None,
        ge=0.0,
        le=100.0
    )
    
    # Applicability
    applies_to_prescription_only: Optional[bool] = None
    applies_to_otc: Optional[bool] = None
    excluded_drug_categories: Optional[List[uuid.UUID]] = None
    excluded_drug_ids: Optional[List[uuid.UUID]] = None
    
    # Price limits
    minimum_price_override: Optional[Decimal] = Field(None, ge=0.0)
    maximum_discount_amount: Optional[Decimal] = Field(None, ge=0.0)
    minimum_purchase_amount: Optional[Decimal] = Field(None, ge=0.0)
    maximum_purchase_amount: Optional[Decimal] = Field(None, ge=0.0)
    
    # Branch applicability
    applies_to_all_branches: Optional[bool] = None
    applicable_branch_ids: Optional[List[uuid.UUID]] = None
    
    # Time validity
    effective_to: Optional[date] = None
    
    # Usage controls
    requires_verification: Optional[bool] = None
    requires_approval: Optional[bool] = None
    allowed_user_roles: Optional[List[str]] = None
    daily_usage_limit: Optional[int] = Field(None, ge=1)
    per_customer_usage_limit: Optional[int] = Field(None, ge=1)
    
    # Insurance fields
    copay_amount: Optional[Decimal] = Field(None, ge=0.0)
    copay_percentage: Optional[Decimal] = Field(None, ge=0.0, le=100.0)
    requires_preauthorization: Optional[bool] = None
    
    # Status
    status: Optional[str] = Field(
        None,
        pattern="^(draft|active|suspended|expired|cancelled)$"
    )
    is_active: Optional[bool] = None
    
    @model_validator(mode='after')
    def validate_update_logic(self) -> 'PriceContractUpdate':
        """Validate update constraints"""
        
        # Validate price limits if both provided
        if self.minimum_purchase_amount and self.maximum_purchase_amount:
            if self.minimum_purchase_amount > self.maximum_purchase_amount:
                raise ValueError("minimum_purchase_amount cannot exceed maximum_purchase_amount")
        
        # Cannot have both copay types
        if self.copay_amount and self.copay_percentage:
            raise ValueError("Cannot specify both copay_amount and copay_percentage")
        
        # Validate branch applicability
        if self.applies_to_all_branches is False and self.applicable_branch_ids is not None:
            if len(self.applicable_branch_ids) == 0:
                raise ValueError("applicable_branch_ids required when applies_to_all_branches is False")
        
        return self


class PriceContractResponse(PriceContractBase, TimestampSchema, SyncSchema):
    """Schema for price contract API responses"""
    
    id: uuid.UUID
    organization_id: uuid.UUID
    
    # ============================================
    # ANALYTICS & METRICS
    # ============================================
    
    usage_count: int = Field(default=0, ge=0, description="Number of times contract was used")
    total_sales_amount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Total sales revenue")
    total_discount_given: Decimal = Field(default=Decimal('0.00'), ge=0, description="Total discounts given")
    average_sale_amount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Average sale amount")
    last_used_at: Optional[datetime] = Field(None, description="Last time contract was used")
    
    unique_customers_count: int = Field(default=0, ge=0, description="Number of unique customers")
    
    # ============================================
    # AUDIT TRAIL
    # ============================================
    
    created_by: uuid.UUID = Field(..., description="User who created the contract")
    approved_by: Optional[uuid.UUID] = Field(None, description="User who approved the contract")
    approved_at: Optional[datetime] = None
    
    last_modified_by: Optional[uuid.UUID] = Field(None, description="User who last modified")
    last_modified_at: Optional[datetime] = None
    
    # ============================================
    # COMPUTED PROPERTIES
    # ============================================
    
    @property
    def is_valid_today(self) -> bool:
        """Check if contract is valid for today's date"""
        today = date.today()
        
        if today < self.effective_from:
            return False
        
        if self.effective_to and today > self.effective_to:
            return False
        
        return self.status == 'active' and self.is_active
    
    @property
    def days_until_expiry(self) -> Optional[int]:
        """Days until contract expires (None if no expiry)"""
        if not self.effective_to:
            return None
        
        delta = self.effective_to - date.today()
        return max(0, delta.days)
    
    @property
    def is_expiring_soon(self) -> bool:
        """Check if contract expires within 30 days"""
        days = self.days_until_expiry
        return days is not None and 0 < days <= 30
    
    @property
    def average_discount_per_sale(self) -> Decimal:
        """Calculate average discount per sale"""
        if self.usage_count == 0:
            return Decimal('0.00')
        
        return round(self.total_discount_given / self.usage_count, 2)
    
    @property
    def discount_rate(self) -> Decimal:
        """Calculate actual discount rate given"""
        if self.total_sales_amount == 0:
            return Decimal('0.00')
        
        return round((self.total_discount_given / self.total_sales_amount) * 100, 2)
    
    model_config = ConfigDict(from_attributes=True)


class PriceContractWithDetails(PriceContractResponse):
    """Price contract with full relationship details"""
    
    # Creator details
    created_by_name: str = Field(..., description="Name of user who created")
    created_by_role: str = Field(..., description="Role of creator")
    
    approved_by_name: Optional[str] = Field(None, description="Name of approver")
    last_modified_by_name: Optional[str] = Field(None, description="Name of last modifier")
    
    # Insurance provider details (if applicable)
    insurance_provider_name: Optional[str] = None
    insurance_provider_code: Optional[str] = None
    insurance_provider_logo_url: Optional[str] = None
    
    # Branch details (if not all branches)
    applicable_branches: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of branch details if applies_to_all_branches is False"
    )
    
    # Contract items count
    custom_pricing_items_count: int = Field(
        default=0,
        ge=0,
        description="Number of drugs with custom pricing overrides"
    )
    
    # Usage statistics breakdown
    usage_by_branch: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Usage statistics per branch"
    )
    
    usage_by_month: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Monthly usage statistics"
    )
    
    top_drugs_sold: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Top 10 drugs sold under this contract"
    )


class PriceContractListResponse(BaseSchema):
    """Response for listing price contracts"""
    contracts: List[PriceContractResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)
    
    # Summary statistics
    total_active_contracts: int = Field(..., ge=0)
    total_suspended_contracts: int = Field(..., ge=0)
    total_expired_contracts: int = Field(..., ge=0)


# ============================================
# Price Contract Item Schemas (Drug-Specific Overrides)
# ============================================

class PriceContractItemBase(BaseSchema):
    """Base fields for contract items (drug-specific pricing)"""
    
    drug_id: uuid.UUID = Field(
        ...,
        description="Drug to apply custom pricing"
    )
    
    override_discount_percentage: Optional[Decimal] = Field(
        None,
        ge=0.0,
        le=100.0,
        description="Override contract discount for this specific drug"
    )
    
    fixed_price: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Set fixed price (ignores base price and discount)"
    )
    
    is_excluded: bool = Field(
        default=False,
        description="Exclude this drug from contract (0% discount)"
    )
    
    notes: Optional[str] = Field(
        None,
        max_length=1000,
        description="Notes about this pricing override"
    )
    
    # Validity period (can be different from contract)
    valid_from: Optional[date] = Field(
        None,
        description="Start date for this item (defaults to contract start)"
    )
    
    valid_to: Optional[date] = Field(
        None,
        description="End date for this item (defaults to contract end)"
    )


class PriceContractItemCreate(PriceContractItemBase):
    """Schema for creating contract item with validation"""
    
    @model_validator(mode='after')
    def validate_pricing(self) -> 'PriceContractItemCreate':
        """Validate pricing configuration"""
        
        # Can't have both fixed_price and override_discount
        if self.fixed_price is not None and self.override_discount_percentage is not None:
            raise ValueError("Cannot specify both fixed_price and override_discount_percentage")
        
        # If excluded, shouldn't have pricing
        if self.is_excluded:
            if self.fixed_price is not None or self.override_discount_percentage is not None:
                raise ValueError("Excluded items cannot have pricing overrides")
        
        # Must have at least one pricing option if not excluded
        if not self.is_excluded:
            if self.fixed_price is None and self.override_discount_percentage is None:
                raise ValueError("Must specify either fixed_price or override_discount_percentage for non-excluded items")
        
        # Validate date range if provided
        if self.valid_from and self.valid_to:
            if self.valid_to < self.valid_from:
                raise ValueError("valid_to must be on or after valid_from")
        
        return self


class PriceContractItemUpdate(BaseSchema):
    """Schema for updating contract item"""
    override_discount_percentage: Optional[Decimal] = Field(None, ge=0.0, le=100.0)
    fixed_price: Optional[Decimal] = Field(None, ge=0.0)
    is_excluded: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=1000)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None


class PriceContractItemResponse(PriceContractItemBase, TimestampSchema):
    """Schema for contract item responses"""
    id: uuid.UUID
    contract_id: uuid.UUID
    
    # Drug details
    drug_name: str
    drug_sku: Optional[str]
    drug_generic_name: Optional[str]
    drug_base_price: Decimal
    drug_category: Optional[str]
    
    model_config = ConfigDict(from_attributes=True)


class PriceContractItemWithCalculations(PriceContractItemResponse):
    """Contract item with price calculations"""
    
    @property
    def effective_price(self) -> Decimal:
        """Calculate effective price for this drug"""
        if self.is_excluded:
            return self.drug_base_price
        
        if self.fixed_price is not None:
            return self.fixed_price
        
        if self.override_discount_percentage is not None:
            discount = self.drug_base_price * (self.override_discount_percentage / 100)
            return self.drug_base_price - discount
        
        # Use contract default discount
        return self.drug_base_price
    
    @property
    def discount_amount(self) -> Decimal:
        """Calculate discount amount"""
        return self.drug_base_price - self.effective_price
    
    @property
    def savings_percentage(self) -> Decimal:
        """Calculate savings percentage"""
        if self.drug_base_price == 0:
            return Decimal('0.00')
        
        return round((self.discount_amount / self.drug_base_price) * 100, 2)


# ============================================
# Contract Filters & Search
# ============================================

class PriceContractFilters(BaseSchema):
    """Advanced filters for searching price contracts"""
    
    contract_type: Optional[str] = Field(
        None,
        pattern="^(insurance|corporate|staff|senior_citizen|standard|wholesale|promotional)$"
    )
    
    status: Optional[str] = Field(
        None,
        pattern="^(draft|active|suspended|expired|cancelled)$"
    )
    
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    
    insurance_provider_id: Optional[uuid.UUID] = None
    
    branch_id: Optional[uuid.UUID] = Field(
        None,
        description="Find contracts applicable to this branch"
    )
    
    user_role: Optional[str] = Field(
        None,
        description="Find contracts available for this user role"
    )
    
    search: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Search in contract_code, contract_name, or description"
    )
    
    valid_on_date: Optional[date] = Field(
        None,
        description="Find contracts valid on specific date"
    )
    
    expiring_within_days: Optional[int] = Field(
        None,
        ge=1,
        le=365,
        description="Find contracts expiring within X days"
    )
    
    min_discount: Optional[Decimal] = Field(None, ge=0.0)
    max_discount: Optional[Decimal] = Field(None, le=100.0)
    
    requires_verification: Optional[bool] = None
    requires_approval: Optional[bool] = None
    
    created_by: Optional[uuid.UUID] = None
    approved_by: Optional[uuid.UUID] = None
    
    # Usage filters
    min_usage_count: Optional[int] = Field(None, ge=0)
    used_in_last_days: Optional[int] = Field(None, ge=1)
    
    # Sorting
    sort_by: Optional[str] = Field(
        default='created_at',
        pattern="^(created_at|contract_name|discount_percentage|usage_count|total_sales_amount|effective_from|last_used_at)$"
    )
    
    sort_order: Optional[str] = Field(
        default='desc',
        pattern="^(asc|desc)$"
    )
    
    # Pagination
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


# ============================================
# Contract Actions
# ============================================

class ApproveContractRequest(BaseSchema):
    """Request to approve a contract for use"""
    notes: Optional[str] = Field(None, max_length=1000)
    auto_activate: bool = Field(
        default=True,
        description="Automatically activate contract after approval"
    )


class SuspendContractRequest(BaseSchema):
    """Request to suspend a contract"""
    reason: str = Field(
        ...,
        min_length=5,
        max_length=1000,
        description="Detailed reason for suspension"
    )
    notify_affected_customers: bool = Field(
        default=True,
        description="Send notification to customers using this contract"
    )


class ActivateContractRequest(BaseSchema):
    """Request to activate a suspended contract"""
    notes: Optional[str] = Field(None, max_length=1000)
    verify_configuration: bool = Field(
        default=True,
        description="Verify contract configuration before activation"
    )


class CancelContractRequest(BaseSchema):
    """Request to permanently cancel a contract"""
    reason: str = Field(
        ...,
        min_length=5,
        max_length=1000,
        description="Reason for cancellation"
    )
    replacement_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Optional replacement contract for affected customers"
    )


class ExtendContractRequest(BaseSchema):
    """Request to extend contract validity period"""
    new_end_date: date = Field(
        ...,
        description="New expiry date for the contract"
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Reason for extension"
    )
    
    @model_validator(mode='after')
    def validate_extension(self) -> 'ExtendContractRequest':
        """Validate extension date is in the future"""
        if self.new_end_date <= date.today():
            raise ValueError("new_end_date must be in the future")
        
        return self


class CloneContractRequest(BaseSchema):
    """Request to clone an existing contract"""
    new_contract_code: str = Field(
        ...,
        min_length=2,
        max_length=50,
        pattern=r'^[A-Z0-9\-]+$'
    )
    new_contract_name: str = Field(
        ...,
        min_length=3,
        max_length=255
    )
    copy_custom_pricing_items: bool = Field(
        default=True,
        description="Copy drug-specific pricing overrides"
    )
    set_as_draft: bool = Field(
        default=True,
        description="Create cloned contract as draft"
    )


# ============================================
# Contract Verification & Eligibility
# ============================================

class VerifyContractEligibilityRequest(BaseSchema):
    """Request to verify if contract can be applied"""
    contract_id: uuid.UUID
    customer_id: Optional[uuid.UUID] = None
    drug_ids: List[uuid.UUID] = Field(default_factory=list)
    branch_id: uuid.UUID
    sale_amount: Optional[Decimal] = Field(None, ge=0.0)


class ContractEligibilityResponse(BaseSchema):
    """Response for contract eligibility check"""
    eligible: bool
    message: str
    contract_name: str
    discount_percentage: Decimal
    
    # Detailed eligibility breakdown
    customer_eligible: bool
    customer_message: Optional[str] = None
    
    branch_eligible: bool
    branch_message: Optional[str] = None
    
    date_eligible: bool
    date_message: Optional[str] = None
    
    amount_eligible: bool
    amount_message: Optional[str] = None
    
    user_role_eligible: bool
    user_role_message: Optional[str] = None
    
    # Drug eligibility (if drugs provided)
    eligible_drugs: List[Dict[str, Any]] = Field(default_factory=list)
    ineligible_drugs: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Requirements
    requires_verification: bool
    requires_approval: bool
    requires_preauthorization: bool
    
    # Insurance details (if applicable)
    insurance_details: Optional[Dict[str, Any]] = None
    copay_amount: Optional[Decimal] = None
    copay_percentage: Optional[Decimal] = None


# ============================================
# Contract Statistics & Analytics
# ============================================

class PriceContractStatistics(BaseSchema):
    """Comprehensive statistics for a price contract"""
    contract_id: uuid.UUID
    contract_name: str
    contract_code: str
    contract_type: str
    discount_percentage: Decimal
    
    # Usage statistics
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    total_discount_given: Decimal = Field(..., ge=0)
    average_sale_amount: Decimal = Field(..., ge=0)
    average_discount_per_sale: Decimal = Field(..., ge=0)
    
    # Effectiveness metrics
    actual_discount_rate: Decimal = Field(
        ...,
        description="Actual discount % given (may differ from contract %)"
    )
    roi: Decimal = Field(..., description="Return on investment for this contract")
    
    # Customer statistics
    unique_customers: int = Field(..., ge=0)
    new_customers: int = Field(..., ge=0)
    returning_customers: int = Field(..., ge=0)
    average_customer_lifetime_value: Decimal = Field(..., ge=0)
    
    # Time statistics
    first_used_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    days_active: int = Field(..., ge=0)
    average_sales_per_day: Decimal = Field(..., ge=0)
    
    # Performance by period
    sales_by_day_of_week: Dict[str, int] = Field(default_factory=dict)
    sales_by_hour: Dict[int, int] = Field(default_factory=dict)
    sales_by_month: Dict[str, Decimal] = Field(default_factory=dict)
    
    # Top performing items
    top_drugs: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Top 20 drugs sold under this contract"
    )
    
    top_categories: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Top drug categories"
    )
    
    # Branch performance
    performance_by_branch: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Usage breakdown by branch"
    )
    
    # Date range
    start_date: datetime
    end_date: datetime
    
    # Comparison to baseline
    baseline_comparison: Optional[Dict[str, Any]] = Field(
        None,
        description="Comparison to baseline (standard pricing)"
    )


class ContractComparison(BaseSchema):
    """Compare multiple contracts side-by-side"""
    contracts: List[PriceContractStatistics]
    comparison_period: str
    
    # Summary comparison metrics
    summary: Dict[str, Any] = Field(
        ...,
        description="Aggregated comparison metrics"
    )
    
    # Winner in each category
    highest_revenue_contract: str
    highest_volume_contract: str
    highest_discount_contract: str
    most_efficient_contract: str
    
    # Recommendations
    recommendations: List[str] = Field(
        default_factory=list,
        description="Strategic recommendations based on analysis"
    )


class ContractPerformanceTrend(BaseSchema):
    """Contract performance over time"""
    contract_id: uuid.UUID
    contract_name: str
    
    # Trend data points (daily, weekly, or monthly)
    trend_data: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Time series data points"
    )
    
    # Trend analysis
    trend_direction: str = Field(
        ...,
        pattern="^(increasing|decreasing|stable|volatile)$"
    )
    
    growth_rate: Decimal = Field(
        ...,
        description="Percentage growth rate"
    )
    
    forecasted_next_period: Decimal = Field(
        ...,
        description="Forecasted revenue for next period"
    )


# ============================================
# Bulk Operations
# ============================================

class BulkContractAction(BaseSchema):
    """Bulk action on multiple contracts"""
    contract_ids: List[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Contracts to perform action on"
    )
    action: str = Field(
        ...,
        pattern="^(activate|suspend|cancel|extend)$"
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=1000
    )


class BulkContractActionResponse(BaseSchema):
    """Response for bulk contract action"""
    total_requested: int = Field(..., ge=0)
    successful: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    
    success_details: List[Dict[str, Any]] = Field(default_factory=list)
    failure_details: List[Dict[str, Any]] = Field(default_factory=list)
    
    message: str


# ============================================
# Export & Import
# ============================================

class ExportContractRequest(BaseSchema):
    """Request to export contract data"""
    contract_ids: Optional[List[uuid.UUID]] = Field(
        None,
        description="Specific contracts to export (None = all)"
    )
    format: str = Field(
        default='json',
        pattern="^(json|csv|excel)$"
    )
    include_statistics: bool = Field(default=True)
    include_pricing_items: bool = Field(default=True)


class ImportContractRequest(BaseSchema):
    """Request to import contracts"""
    data: str = Field(
        ...,
        description="Contract data in JSON format"
    )
    skip_validation: bool = Field(
        default=False,
        description="Skip validation (not recommended)"
    )
    update_existing: bool = Field(
        default=False,
        description="Update contracts if they already exist"
    )