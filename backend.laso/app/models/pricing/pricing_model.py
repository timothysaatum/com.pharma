from app.db.base import Base
from sqlalchemy import (
    String, Integer, Boolean, DateTime, Numeric, Text,
    ForeignKey, Index, CheckConstraint, UniqueConstraint, Date, text
)
from app.models.db_types import UUID, ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, date
import uuid

from app.models.core.mixins import TimestampMixin, SyncTrackingMixin, SoftDeleteMixin

if TYPE_CHECKING:
    from app.models.pharmacy.pharmacy_model import Organization


class PriceContract(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """
    Price contracts for insurance companies, corporate clients, and discount programs.
    
    - All approval-related functionality
    
    USAGE:
    - Personnel can freely select any active contract during checkout
    - Contract applies discount to all applicable items
    - No additional manual discounts allowed beyond contract
    
    Examples:
    - "GLICO Insurance Standard Contract" (10% discount)
    - "SIC Insurance Premium Plan" (15% discount)
    - "Senior Citizens Discount" (5% discount)
    - "Staff Discount Program" (20% discount)
    - "Standard Retail Pricing" (0% discount - default)
    """
    __tablename__ = 'price_contracts'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    # ==================== CONTRACT IDENTIFICATION ====================
    
    contract_code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Unique code: GLICO-STD, SIC-PREM, STAFF-20, SENIOR-5, STANDARD"
    )
    
    contract_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Display name: 'GLICO Insurance Standard Plan'"
    )
    
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="Contract terms and conditions"
    )
    
    # ==================== CONTRACT TYPE ====================
    
    contract_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="insurance, corporate, staff, senior_citizen, standard, wholesale"
    )
    
    is_default_contract: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="TRUE for 'Standard Pricing' contract used as fallback"
    )
    
    # ==================== DISCOUNT CONFIGURATION ====================
    
    discount_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default='percentage',
        comment="percentage, fixed_amount, custom"
    )
    
    discount_percentage: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0.00,
        nullable=False,
        comment="Discount percentage: 5.00, 10.00, 15.00, 20.00"
    )
    
    # ==================== APPLICABILITY RULES ====================
    
    # Drug type restrictions
    applies_to_prescription_only: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="If TRUE, contract only applies to prescription drugs"
    )
    
    applies_to_otc: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="If TRUE, contract applies to over-the-counter drugs"
    )
    
    # Exclusions
    excluded_drug_categories: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        default=list,
        comment="Categories excluded from this contract (e.g., controlled substances)"
    )
    
    excluded_drug_ids: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        default=list,
        comment="Specific drugs excluded from contract"
    )
    
    # Price limits
    minimum_price_override: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Never go below this price even with discount"
    )
    
    maximum_discount_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Cap the discount amount per item"
    )
    
    # Purchase amount limits for the entire transaction
    minimum_purchase_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Minimum total purchase amount required for this contract"
    )
    
    maximum_purchase_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Maximum total purchase amount allowed under this contract"
    )
    
    # ==================== BRANCH APPLICABILITY ====================
    
    applies_to_all_branches: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="If FALSE, only specific branches can use this contract"
    )
    
    applicable_branch_ids: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        default=list,
        comment="Specific branches where this contract is valid"
    )
    
    # ==================== TIME VALIDITY ====================
    
    effective_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        comment="Contract start date"
    )
    
    effective_to: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        index=True,
        comment="Contract end date (NULL = no expiry)"
    )
    
    # ==================== USAGE CONTROLS (SIMPLIFIED) ====================
    
    requires_verification: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Require verification (e.g., insurance card scan) before applying"
    )
        
    allowed_user_roles: Mapped[List[str]] = mapped_column(
        ARRAY(String),
        default=list,
        comment="User roles allowed to apply this contract: ['pharmacist', 'cashier', 'manager']. Empty = all roles"
    )
    
    # ==================== INSURANCE-SPECIFIC FIELDS ====================
    
    insurance_provider_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('insurance_providers.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Link to insurance provider if contract_type = 'insurance'"
    )
    
    copay_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Fixed copay amount patient must pay"
    )
    
    copay_percentage: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        comment="Percentage of price patient must pay"
    )
    
    # ==================== AUTHORIZATION & AUDIT ====================
    
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False,
        comment="User who created the contract"
    )
    
    # Contract approval (for creating the contract itself, not for applying it during sales)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        comment="Manager who approved this contract for use"
    )
    
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    
    # ==================== STATUS ====================
    
    status: Mapped[str] = mapped_column(
        String(50),
        default='draft',
        nullable=False,
        index=True,
        comment="draft, active, suspended, expired, cancelled"
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # ==================== ANALYTICS ====================
    
    total_transactions: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of sales using this contract"
    )
    
    total_discount_given: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False,
        comment="Total amount discounted across all sales"
    )
    
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="Last time this contract was used in a sale"
    )
    
    # ==================== RELATIONSHIPS ====================
    
    insurance_provider: Mapped[Optional["InsuranceProvider"]] = relationship(back_populates="contracts")
    contract_items: Mapped[List["PriceContractItem"]] = relationship(
        back_populates="contract",
        cascade="all, delete-orphan"
    )
    
    # ==================== TABLE CONSTRAINTS ====================
    
    __table_args__ = (
        CheckConstraint(
            "contract_type IN ('insurance', 'corporate', 'staff', 'senior_citizen', 'standard', 'wholesale')",
            name='check_contract_type'
        ),
        CheckConstraint(
            "discount_type IN ('percentage', 'fixed_amount', 'custom')",
            name='check_discount_type'
        ),
        CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name='check_discount_percentage_range'
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'suspended', 'expired', 'cancelled')",
            name='check_contract_status'
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name='check_contract_dates'
        ),
        
        # Indexes for performance
        Index('idx_contract_org', 'organization_id'),
        Index('idx_contract_code', 'contract_code'),
        Index('idx_contract_type', 'contract_type'),
        Index('idx_contract_status', 'status', 'is_active'),
        Index('idx_contract_insurance', 'insurance_provider_id'),
        Index('idx_contract_dates', 'effective_from', 'effective_to'),
        
        # Partial unique index for default contract (only one default per org)
        Index(
            'idx_contract_default_unique', 
            'organization_id', 
            'is_default_contract',
            unique=True,
            postgresql_where=text('is_default_contract = TRUE')
        ),
        
        # Partial index for active contracts
        Index(
            'idx_contract_active_dates', 
            'organization_id', 'is_active', 'status', 'effective_from', 'effective_to',
            postgresql_where=text("is_active = TRUE AND status = 'active'")
        ),
    )
    
    # ==================== METHODS ====================
    
    def is_valid_for_date(self, check_date: Optional[date] = None) -> bool:
        """Check if contract is valid on given date."""
        check_date = check_date or date.today()
        
        if check_date < self.effective_from:
            return False
        
        if self.effective_to and check_date > self.effective_to:
            return False
        
        return True
    
    def is_applicable_to_branch(self, branch_id: uuid.UUID) -> bool:
        """Check if contract applies to given branch."""
        if self.applies_to_all_branches:
            return True
        
        return branch_id in self.applicable_branch_ids
    
    def is_applicable_to_drug(self, drug_id: uuid.UUID, drug_type: str, category_id: Optional[uuid.UUID] = None) -> bool:
        """
        Check if contract applies to a specific drug.
        
        Args:
            drug_id: UUID of the drug
            drug_type: 'prescription' or 'otc'
            category_id: Optional category UUID
        
        Returns:
            True if contract can be applied to this drug
        """
        # Check if drug is explicitly excluded
        if drug_id in self.excluded_drug_ids:
            return False
        
        # Check if drug's category is excluded
        if category_id and category_id in self.excluded_drug_categories:
            return False
        
        # Check drug type restrictions
        if self.applies_to_prescription_only and drug_type != 'prescription':
            return False
        
        if not self.applies_to_otc and drug_type == 'otc':
            return False
        
        return True
    
    def can_be_applied_by_user(self, user_role: str) -> bool:
        """
        Check if a user with given role can apply this contract.
        
        Args:
            user_role: Role of the user (e.g., 'cashier', 'pharmacist')
        
        Returns:
            True if user can apply this contract
        """
        # If no role restrictions, anyone can use it
        if not self.allowed_user_roles:
            return True
        
        return user_role in self.allowed_user_roles
    
    def calculate_discount(self, original_price: float) -> float:
        """
        Calculate discount amount for a given price.
        
        Args:
            original_price: Base price before discount
        
        Returns:
            Discount amount to subtract from price
        """
        if self.discount_type == 'percentage':
            discount = original_price * (float(self.discount_percentage) / 100)
        elif self.discount_type == 'fixed_amount':
            # Reusing discount_percentage field for fixed amount
            discount = float(self.discount_percentage)
        else:
            discount = 0.00
        
        # Apply maximum discount cap if set
        if self.maximum_discount_amount and discount > float(self.maximum_discount_amount):
            discount = float(self.maximum_discount_amount)
        
        return round(discount, 2)
    
    def calculate_final_price(self, original_price: float) -> float:
        """
        Calculate final price after applying contract discount.
        
        Args:
            original_price: Base price before discount
        
        Returns:
            Final price after discount
        """
        discount = self.calculate_discount(original_price)
        final_price = original_price - discount
        
        # Apply minimum price override if set
        if self.minimum_price_override and final_price < float(self.minimum_price_override):
            final_price = float(self.minimum_price_override)
        
        return round(final_price, 2)


# ==================== INSURANCE PROVIDER MODEL ====================

class InsuranceProvider(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """
    Insurance companies and their details.
    """
    __tablename__ = 'insurance_providers'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    # Provider information
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Insurance company name: GLICO, SIC, Enterprise, etc."
    )
    
    code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="Short code: GLICO, SIC, ENT"
    )
    
    logo_url: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="URL to insurance company logo"
    )
    
    # Contact information
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    website: Mapped[Optional[str]] = mapped_column(String(255))
    
    address: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="Insurance company address"
    )
    
    # Contract information
    primary_contact_name: Mapped[Optional[str]] = mapped_column(String(255))
    primary_contact_phone: Mapped[Optional[str]] = mapped_column(String(20))
    primary_contact_email: Mapped[Optional[str]] = mapped_column(String(255))
    
    # Billing settings
    billing_cycle: Mapped[str] = mapped_column(
        String(50),
        default='monthly',
        comment="daily, weekly, monthly, quarterly"
    )
    
    payment_terms: Mapped[str] = mapped_column(
        String(50),
        default='NET30',
        comment="NET15, NET30, NET60, etc."
    )
    
    # Verification settings
    requires_card_verification: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Require insurance card scan/verification"
    )
    
    requires_preauth: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Require pre-authorization for claims"
    )
    
    verification_endpoint: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="API endpoint for real-time eligibility verification"
    )
    
    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Relationships
    contracts: Mapped[List["PriceContract"]] = relationship(back_populates="insurance_provider")
    
    __table_args__ = (
        CheckConstraint(
            "billing_cycle IN ('daily', 'weekly', 'monthly', 'quarterly', 'annually')",
            name='check_billing_cycle'
        ),
        Index('idx_insurance_org', 'organization_id'),
        Index('idx_insurance_code', 'code'),
        Index('idx_insurance_active', 'is_active'),
    )


# ==================== PRICE CONTRACT ITEM (Optional - For Drug-Specific Pricing) ====================

class PriceContractItem(Base, TimestampMixin):
    """
    Optional: Override contract discount for specific drugs.
    
    Example: Under GLICO contract, most drugs get 10% off,
    but Drug X gets special 15% off, and Drug Y gets 5% off.
    """
    __tablename__ = 'price_contract_items'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('price_contracts.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drugs.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    # Override pricing for this specific drug
    override_discount_percentage: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        comment="Override the contract's default discount for this drug"
    )
    
    fixed_price: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Set a fixed price for this drug (ignores base price and discount)"
    )
    
    is_excluded: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Exclude this drug from contract (0% discount)"
    )
    
    notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Relationships
    contract: Mapped["PriceContract"] = relationship(back_populates="contract_items")
    
    __table_args__ = (
        UniqueConstraint('contract_id', 'drug_id', name='uq_contract_drug'),
        CheckConstraint(
            "override_discount_percentage IS NULL OR (override_discount_percentage >= 0 AND override_discount_percentage <= 100)",
            name='check_override_discount'
        ),
        Index('idx_contract_item_contract', 'contract_id'),
        Index('idx_contract_item_drug', 'drug_id'),
    )