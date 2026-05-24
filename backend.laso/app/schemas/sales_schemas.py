"""
Sales Schemas

Features:
- Comprehensive validation
- Contract eligibility verification
- Insurance handling
- Prescription management
- Audit trail
- Security controls
"""
from pydantic import Field, field_validator, model_validator, ConfigDict, computed_field
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal
import uuid

from app.schemas.base_schemas import BaseSchema, TimestampSchema, SyncSchema


# ============================================
# Sale Item Schemas
# ============================================

class SaleItemBase(BaseSchema):
    """Base sale item fields with validation"""
    drug_id: uuid.UUID = Field(
        ...,
        description="Drug being sold"
    )
    
    quantity: int = Field(
        ...,
        gt=0,
        le=1000,
        description="Quantity sold (max 1000 per item for fraud prevention)"
    )
    
    batch_id: Optional[uuid.UUID] = Field(
        None,
        description="Specific batch to use (system auto-selects if not provided using FEFO)"
    )


class SaleItemCreate(SaleItemBase):
    """
    Schema for creating a sale item.
    
    Pricing is calculated server-side using contract and drug data.
    User only provides drug_id, quantity, and optional batch_id.
    """
    requires_prescription: bool = Field(
        default=False,
        description="Whether this item requires a prescription"
    )
    
    prescription_verified: bool = Field(
        default=False,
        description="Whether prescription has been verified for this item"
    )
    
    @model_validator(mode='after')
    def validate_prescription(self) -> 'SaleItemCreate':
        """Validate prescription requirements"""
        if self.requires_prescription and not self.prescription_verified:
            raise ValueError(
                f"Prescription verification required for this drug (drug_id: {self.drug_id})"
            )
        
        return self


class SaleItemResponse(TimestampSchema):
    """Schema for sale item API responses"""
    id: uuid.UUID
    sale_id: uuid.UUID
    drug_id: uuid.UUID
    drug_name: str
    drug_sku: Optional[str] = None
    drug_generic_name: Optional[str] = None
    
    # Quantity and batch tracking
    quantity: int
    batch_id: Optional[uuid.UUID] = None
    batch_number: Optional[str] = Field(None, description="Batch number for traceability")
    batch_expiry_date: Optional[date] = Field(None, description="Batch expiry date")
    
    # ============================================
    # PRICING BREAKDOWN (Calculated by server)
    # ============================================
    
    unit_price: Decimal = Field(
        ...,
        description="Base unit price from Drug model"
    )
    
    subtotal: Decimal = Field(
        ...,
        description="unit_price × quantity (before discount and tax)"
    )
    
    # Contract discount
    contract_discount_percentage: Decimal = Field(
        default=Decimal('0.00'),
        description="Discount % from price contract applied to this item"
    )
    
    contract_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        description="Discount amount from contract"
    )
    
    # Additional discount (with manager approval)
    additional_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        description="Additional manual discount (requires manager approval)"
    )
    
    # Total discount
    total_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        validation_alias='discount_amount',
        description="contract_discount_amount + additional_discount_amount"
    )
    
    # Tax calculation
    tax_rate: Decimal = Field(..., description="Tax rate percentage")
    tax_amount: Decimal = Field(..., description="Calculated tax on discounted price")
    
    # Final price
    total_price: Decimal = Field(
        ...,
        description="Final price: (subtotal - total_discount_amount + tax_amount)"
    )
    
    # ============================================
    # CONTRACT & INSURANCE TRACKING
    # ============================================
    
    applied_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Price contract applied to this item"
    )
    
    applied_contract_name: Optional[str] = Field(
        None,
        description="Contract name (snapshot for history)"
    )
    
    insurance_covered: bool = Field(
        default=False,
        description="Whether this item is covered by insurance"
    )
    
    patient_copay: Optional[Decimal] = Field(
        None,
        description="Patient copay amount for insurance"
    )
    
    # ============================================
    # PRESCRIPTION & SAFETY
    # ============================================
    
    requires_prescription: bool = False
    prescription_verified: bool = False
    prescription_id: Optional[uuid.UUID] = None
    
    # Allergy warnings checked
    allergy_check_performed: bool = Field(
        default=False,
        description="Whether customer allergy check was performed"
    )
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class SaleItemWithDetails(SaleItemResponse):
    """Sale item with additional details"""
    drug_manufacturer: Optional[str] = None
    drug_category: Optional[str] = None
    pharmacist_notes: Optional[str] = Field(
        None,
        description="Pharmacist notes about this item"
    )


# ============================================
# Sale Main Schemas
# ============================================

class SaleBase(BaseSchema):
    """Base sale fields"""
    customer_id: Optional[uuid.UUID] = Field(
        None,
        description="Registered customer ID (required for insurance/corporate contracts)"
    )
    
    customer_name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="For walk-in customers without registration"
    )
    
    payment_method: str = Field(
        ...,
        pattern="^(cash|card|mobile_money|insurance|credit|split)$",
        description="Primary payment method"
    )
    
    prescription_id: Optional[uuid.UUID] = Field(
        None,
        description="Linked prescription if applicable"
    )
    
    notes: Optional[str] = Field(
        None,
        max_length=1000,
        description="Additional notes about the sale"
    )


class SaleCreate(SaleBase):
    """
    Schema for creating a sale with comprehensive validation.
    
    CRITICAL FLOW:
    1. Cashier scans/selects drugs
    2. System suggests eligible contracts based on customer
    3. Cashier selects price contract
    4. System calculates discounts and validates
    5. Payment is processed
    """
    
    branch_id: uuid.UUID = Field(
        ...,
        description="Branch where sale is occurring"
    )
    
    # ============================================
    # PRICING CONTRACT (REQUIRED)
    # ============================================
    
    price_contract_id: uuid.UUID = Field(
        ...,
        description="REQUIRED: Price contract selected by cashier"
    )
    
    contract_verification_token: Optional[str] = Field(
        None,
        description="Verification token if contract requires verification (e.g., insurance card scan)"
    )
    
        
    # ============================================
    # INSURANCE HANDLING
    # ============================================
    
    insurance_verified: bool = Field(
        default=False,
        description="Whether insurance eligibility was verified"
    )
    
    insurance_claim_number: Optional[str] = Field(
        None,
        max_length=100,
        description="Insurance claim reference number"
    )
    
    insurance_preauth_number: Optional[str] = Field(
        None,
        max_length=100,
        description="Pre-authorization number from insurance"
    )
    
    # ============================================
    # SALE ITEMS
    # ============================================
    
    items: List[SaleItemCreate] = Field(
        ...,
        min_length=1,
        max_length=100,  # Max 100 items per sale
        description="Items being sold"
    )
    
    # ============================================
    # PAYMENT DETAILS
    # ============================================
    
    amount_paid: Optional[Decimal] = Field(
        None,
        ge=0.0,
        description="Amount paid by customer (calculated if not provided)"
    )
    
    payment_reference: Optional[str] = Field(
        None,
        max_length=255,
        description="Payment gateway transaction ID"
    )
    
    split_payment_details: Optional[Dict[str, Decimal]] = Field(
        None,
        description="Details if payment method is 'split': {cash: 100.00, card: 50.00}"
    )
    
    # ============================================
    # VALIDATORS
    # ============================================
    
    @field_validator('items')
    @classmethod
    def validate_items_count(cls, v: List[SaleItemCreate]) -> List[SaleItemCreate]:
        """Validate items list"""
        if len(v) == 0:
            raise ValueError("Sale must have at least one item")
        
        if len(v) > 100:
            raise ValueError("Maximum 100 items per sale")
        
        # Check for duplicate drugs
        drug_ids = [item.drug_id for item in v]
        if len(drug_ids) != len(set(drug_ids)):
            raise ValueError("Duplicate drugs in sale items. Combine quantities instead.")
        
        return v
    
    @model_validator(mode='after')
    def validate_prescription_requirements(self) -> 'SaleCreate':
        """Validate prescription requirements"""
        has_prescription_items = any(item.requires_prescription for item in self.items)
        
        if has_prescription_items and not self.prescription_id:
            raise ValueError(
                "Prescription ID required when selling prescription drugs"
            )
        
        return self
    
    @model_validator(mode='after')
    def validate_customer_for_contract(self) -> 'SaleCreate':
        """Validate customer requirements for specific contract types"""
        # Insurance and corporate contracts require registered customer
        # This will be further validated on the server side against the actual contract
        
        if not self.customer_id and not self.customer_name:
            raise ValueError(
                "Either customer_id (registered) or customer_name (walk-in) required"
            )
        
        return self
    
    @model_validator(mode='after')
    def validate_insurance_fields(self) -> 'SaleCreate':
        """Validate insurance-related fields"""
        if self.payment_method == 'insurance':
            if not self.insurance_verified:
                raise ValueError(
                    "Insurance must be verified before processing payment"
                )
            
            if not self.customer_id:
                raise ValueError(
                    "Registered customer required for insurance payments"
                )
        
        return self
    
    @model_validator(mode='after')
    def validate_split_payment(self) -> 'SaleCreate':
        """Validate split payment details"""
        if self.payment_method == 'split':
            if not self.split_payment_details:
                raise ValueError(
                    "split_payment_details required when payment_method is 'split'"
                )
            
            if len(self.split_payment_details) < 2:
                raise ValueError(
                    "Split payment must have at least 2 payment methods"
                )
            
            # Validate split payment methods
            valid_methods = {'cash', 'card', 'mobile_money', 'insurance', 'credit'}
            for method in self.split_payment_details.keys():
                if method not in valid_methods:
                    raise ValueError(f"Invalid payment method in split: {method}")
        
        return self


class SaleUpdate(BaseSchema):
    """Schema for updating a sale (very limited after creation)"""
    notes: Optional[str] = Field(None, max_length=1000)
    
    # Can only update payment status in specific transitions
    payment_status: Optional[str] = Field(
        None,
        pattern="^(pending|completed|partial|refunded|cancelled)$"
    )


class SaleResponse(SaleBase, TimestampSchema, SyncSchema):
    """Schema for sale API responses"""
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    sale_number: str = Field(..., description="Unique sale reference number")
    
    # ============================================
    # FINANCIAL SUMMARY
    # ============================================
    
    subtotal: Decimal = Field(
        ...,
        description="Sum of all items before discount and tax"
    )
    
    # Discount breakdown
    contract_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        description="Total discount from price contract"
    )
    
    additional_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        description="Additional manual discount (with manager approval)"
    )
    
    total_discount_amount: Decimal = Field(
        default=Decimal('0.00'),
        validation_alias='discount_amount',
        description="contract_discount_amount + additional_discount_amount"
    )
    
    # Tax and total
    tax_amount: Decimal = Field(..., description="Total tax on discounted price")
    total_amount: Decimal = Field(..., description="Final amount to pay")
    
    # ============================================
    # CONTRACT INFORMATION
    # ============================================
    
    price_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Price contract that was applied"
    )
    
    contract_name: Optional[str] = Field(
        None,
        description="Contract name (snapshot for history)"
    )
    
    contract_type: Optional[str] = Field(
        None,
        description="Contract type (snapshot)"
    )
    
    contract_discount_percentage: Optional[Decimal] = Field(
        None,
        description="Discount percentage from contract (snapshot)"
    )
    
    # ============================================
    # INSURANCE DETAILS
    # ============================================
    
    insurance_claim_number: Optional[str] = None
    insurance_preauth_number: Optional[str] = None
    
    patient_copay_amount: Optional[Decimal] = Field(
        None,
        description="Amount patient paid as copay"
    )
    
    insurance_covered_amount: Optional[Decimal] = Field(
        None,
        description="Amount covered by insurance"
    )
    
    insurance_verified: bool = False
    insurance_verified_at: Optional[datetime] = None
    insurance_verified_by: Optional[uuid.UUID] = None
    
    # ============================================
    # PAYMENT DETAILS
    # ============================================
    
    payment_status: str = Field(
        default='completed',
        description="Payment status"
    )
    
    amount_paid: Decimal = Field(..., description="Actual amount paid")
    change_amount: Decimal = Field(default=Decimal('0.00'), description="Change given")
    payment_reference: Optional[str] = None
    split_payment_details: Optional[Dict[str, Decimal]] = None
    
    # ============================================
    # STAFF TRACKING
    # ============================================
    
    cashier_id: uuid.UUID = Field(..., description="User who processed the sale")
    cashier_name: Optional[str] = Field(None, description="Cashier name (snapshot)")
    
    pharmacist_id: Optional[uuid.UUID] = Field(
        None,
        description="Pharmacist who verified prescription"
    )
    pharmacist_name: Optional[str] = None
    
    manager_approval_user_id: Optional[uuid.UUID] = Field(
        None,
        description="Manager who approved additional discount"
    )
    
    # ============================================
    # STATUS & AUDIT
    # ============================================
    
    status: str = Field(
        default='completed',
        pattern="^(draft|completed|cancelled|refunded)$"
    )
    
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[uuid.UUID] = None
    cancellation_reason: Optional[str] = None
    
    refund_amount: Optional[Decimal] = None
    refunded_at: Optional[datetime] = None
    refunded_by: Optional[uuid.UUID] = None
    
    # ============================================
    # COMPUTED PROPERTIES
    # ============================================
    
    @property
    def effective_discount_rate(self) -> Decimal:
        """Calculate actual discount rate given"""
        if self.subtotal == 0:
            return Decimal('0.00')
        
        return round((self.total_discount_amount / self.subtotal) * 100, 2)
    
    @property
    def net_amount(self) -> Decimal:
        """Amount after all deductions"""
        return self.total_amount - (self.insurance_covered_amount or Decimal('0.00'))

    @computed_field  # type: ignore[misc]
    @property
    def items_count(self) -> int:
        """Always 0 for list responses — items are not loaded by the list endpoint.
        Overridden by SaleWithDetails which has the actual items list."""
        return 0

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class SaleWithDetails(SaleResponse):
    """Sale with complete item details"""
    items: List[SaleItemWithDetails] = Field(
        default_factory=list,
        description="Detailed line items"
    )

    # Customer details
    customer_full_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    customer_loyalty_tier: Optional[str] = None

    # Branch details
    branch_name: str
    branch_address: Optional[Dict[str, Any]] = None

    # Organization details
    organization_name: str
    organization_tax_id: Optional[str] = None

    @computed_field  # type: ignore[misc]
    @property
    def items_count(self) -> int:
        """Derived from items list — never stored, never triggers validate_assignment."""
        return len(self.items)


class ProcessSaleResponse(BaseSchema):
    """Response after processing a sale with comprehensive details"""
    sale: SaleWithDetails
    
    # Operation results
    inventory_updated: int = Field(
        ...,
        ge=0,
        description="Number of inventory records updated"
    )
    
    batches_updated: int = Field(
        ...,
        ge=0,
        description="Number of batches deducted (FEFO)"
    )
    
    # Loyalty program
    loyalty_points_awarded: int = Field(
        default=0,
        ge=0,
        description="Loyalty points awarded to customer"
    )
    
    loyalty_tier_upgraded: bool = Field(
        default=False,
        description="Whether customer was upgraded to new loyalty tier"
    )
    
    new_loyalty_tier: Optional[str] = None
    
    # Alerts generated
    low_stock_alerts_created: int = Field(
        default=0,
        ge=0,
        description="Number of low stock alerts generated"
    )
    
    expiry_alerts_created: int = Field(
        default=0,
        ge=0,
        description="Number of near-expiry alerts generated"
    )
    
    # Contract information
    contract_applied: str = Field(
        ...,
        description="Name of price contract that was applied"
    )
    
    contract_discount_given: Decimal = Field(
        ...,
        description="Total discount from contract"
    )
    
    estimated_savings: Decimal = Field(
        ...,
        description="Savings compared to standard pricing"
    )
    
    # Success status
    success: bool = True
    message: str = "Sale processed successfully"
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-critical warnings (e.g., low stock, near expiry)"
    )


# ============================================
# Refund & Cancel Operations
# ============================================

class RefundItemData(BaseSchema):
    """Data for refunding a sale item"""
    sale_item_id: uuid.UUID = Field(..., description="Sale item to refund")
    quantity: int = Field(..., gt=0, description="Quantity to refund")
    reason: str = Field(..., min_length=5, max_length=500)
    
    restock: bool = Field(
        default=True,
        description="Whether to return items to inventory"
    )


class RefundSaleRequest(BaseSchema):
    """Request to refund a sale (full or partial)"""
    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Detailed refund reason"
    )
    
    items_to_refund: List[RefundItemData] = Field(
        ...,
        min_length=1,
        description="Items to refund"
    )
    
    refund_amount: Decimal = Field(
        ...,
        ge=0.0,
        description="Amount to refund (calculated from items)"
    )
    
    refund_method: str = Field(
        default='original',
        pattern="^(original|cash|store_credit)$",
        description="Method of refund"
    )
    
    manager_approval_user_id: uuid.UUID = Field(
        ...,
        description="Manager approving the refund"
    )
    
    @field_validator('items_to_refund')
    @classmethod
    def validate_items(cls, v: List[RefundItemData]) -> List[RefundItemData]:
        """Ensure at least one item"""
        if len(v) == 0:
            raise ValueError("Must refund at least one item")
        
        # Check for duplicates
        item_ids = [item.sale_item_id for item in v]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Duplicate items in refund list")
        
        return v


class RefundSaleResponse(BaseSchema):
    """Response after refunding sale"""
    sale: SaleWithDetails
    
    refund_id: uuid.UUID = Field(..., description="Unique refund transaction ID")
    refund_amount: Decimal
    refund_method: str
    
    # Inventory restoration
    inventory_restored: int = Field(..., ge=0)
    batches_restored: int = Field(..., ge=0)
    
    # Loyalty points
    loyalty_points_deducted: int = Field(default=0, ge=0)
    
    success: bool = True
    message: str = "Sale refunded successfully"


class CancelSaleRequest(BaseSchema):
    """Request to cancel a sale"""
    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Detailed cancellation reason"
    )
    
    manager_approval_user_id: uuid.UUID = Field(
        ...,
        description="Manager approving the cancellation"
    )
    
    restore_inventory: bool = Field(
        default=True,
        description="Restore items to inventory"
    )


# ============================================
# Receipt Generation
# ============================================

class ReceiptData(BaseSchema):
    """Receipt data for printing/emailing"""
    sale: SaleWithDetails
    
    # Organization info
    organization_name: str
    organization_tax_id: Optional[str] = None
    
    # Branch info
    branch_name: str
    branch_address: Optional[Dict[str, Any]] = None
    branch_phone: Optional[str] = None
    branch_email: Optional[str] = None
    
    # Receipt details
    receipt_number: str
    receipt_date: datetime
    cashier_name: str
    
    # QR code for verification
    qr_code_data: Optional[str] = Field(
        None,
        description="QR code data for receipt verification"
    )
    
    # Footer message
    footer_message: Optional[str] = Field(
        None,
        description="Custom footer message (e.g., 'Thank you for your purchase')"
    )
    
    @property
    def receipt_items(self) -> List[Dict[str, Any]]:
        """Format items for receipt"""
        return [
            {
                "name": item.drug_name,
                "generic_name": item.drug_generic_name,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "subtotal": float(item.subtotal),
                "discount": float(item.total_discount_amount),
                "tax": float(item.tax_amount),
                "total": float(item.total_price),
                "batch_number": item.batch_number,
            }
            for item in self.sale.items
        ]
    
    @property
    def payment_breakdown(self) -> Dict[str, Any]:
        """Comprehensive payment breakdown"""
        breakdown = {
            "subtotal": float(self.sale.subtotal),
            "contract_discount": float(self.sale.contract_discount_amount),
            "additional_discount": float(self.sale.additional_discount_amount),
            "total_discount": float(self.sale.total_discount_amount),
            "discount_percentage": float(self.sale.effective_discount_rate),
            "tax": float(self.sale.tax_amount),
            "total": float(self.sale.total_amount),
            "paid": float(self.sale.amount_paid),
            "change": float(self.sale.change_amount),
            "payment_method": self.sale.payment_method,
        }
        
        # Add insurance details if applicable
        if self.sale.insurance_claim_number:
            breakdown["insurance"] = {
                "claim_number": self.sale.insurance_claim_number,
                "copay": float(self.sale.patient_copay_amount or 0),
                "insurance_covered": float(self.sale.insurance_covered_amount or 0),
                "preauth_number": self.sale.insurance_preauth_number,
            }
        
        # Add contract details
        if self.sale.contract_name:
            breakdown["contract"] = {
                "name": self.sale.contract_name,
                "type": self.sale.contract_type,
                "discount": float(self.sale.contract_discount_amount),
            }
        
        # Add split payment details
        if self.sale.split_payment_details:
            breakdown["split_payment"] = {
                method: float(amount)
                for method, amount in self.sale.split_payment_details.items()
            }
        
        return breakdown


class EmailReceiptRequest(BaseSchema):
    """Request to email receipt to customer"""
    sale_id: uuid.UUID
    email: str = Field(
        ...,
        pattern=r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    )
    include_detailed_breakdown: bool = Field(
        default=True,
        description="Include detailed item breakdown"
    )


class PrintReceiptRequest(BaseSchema):
    """Request to print receipt"""
    sale_id: uuid.UUID
    printer_id: Optional[str] = Field(
        None,
        description="Specific printer to use"
    )
    copies: int = Field(default=1, ge=1, le=5)


# ============================================
# Contract Switching During Checkout
# ============================================

class SwitchContractRequest(BaseSchema):
    """Request to switch contract on draft sale"""
    new_contract_id: uuid.UUID = Field(
        ...,
        description="New contract to apply"
    )
    
    reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Reason for switching contract"
    )
    
    verification_token: Optional[str] = Field(
        None,
        description="Verification token if new contract requires verification"
    )


class SwitchContractResponse(BaseSchema):
    """Response after switching contract"""
    success: bool
    message: str
    
    old_contract_name: Optional[str]
    new_contract_name: str
    
    # Financial impact
    old_total_amount: Decimal
    new_total_amount: Decimal
    difference: Decimal
    
    # New discount breakdown
    new_contract_discount: Decimal
    new_total_discount: Decimal


# ============================================
# Filtering & Search
# ============================================

class SaleFilters(BaseSchema):
    """Advanced filters for sale queries"""
    
    # Date range
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    # Status filters
    status: Optional[str] = Field(
        None,
        pattern="^(draft|completed|cancelled|refunded)$"
    )
    
    payment_status: Optional[str] = Field(
        None,
        pattern="^(pending|completed|partial|refunded|cancelled)$"
    )
    
    payment_method: Optional[str] = Field(
        None,
        pattern="^(cash|card|mobile_money|insurance|credit|split)$"
    )
    
    # Entity filters
    branch_id: Optional[uuid.UUID] = None
    cashier_id: Optional[uuid.UUID] = None
    customer_id: Optional[uuid.UUID] = None
    pharmacist_id: Optional[uuid.UUID] = None
    
    # Contract filters
    price_contract_id: Optional[uuid.UUID] = Field(
        None,
        description="Filter by specific price contract"
    )
    
    contract_type: Optional[str] = Field(
        None,
        pattern="^(insurance|corporate|staff|senior_citizen|standard|wholesale|promotional)$"
    )
    
    # Amount filters
    min_amount: Optional[Decimal] = Field(None, ge=0.0)
    max_amount: Optional[Decimal] = Field(None, ge=0.0)
    
    # Insurance filters
    has_insurance: Optional[bool] = None
    insurance_verified: Optional[bool] = None
    
    # Prescription filters
    has_prescription: Optional[bool] = None
    
    # Search
    search: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Search in sale_number, customer_name, claim_number"
    )
    
    # Sorting
    sort_by: Optional[str] = Field(
        default='created_at',
        pattern="^(created_at|sale_number|total_amount|customer_name|contract_name)$"
    )
    
    sort_order: Optional[str] = Field(
        default='desc',
        pattern="^(asc|desc)$"
    )
    
    # Pagination
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    
    @model_validator(mode='after')
    def validate_filters(self) -> 'SaleFilters':
        """Validate filter combinations"""
        
        # Validate date range
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("end_date must be after start_date")
            
            delta = self.end_date - self.start_date
            if delta.days > 365:
                raise ValueError("Date range cannot exceed 365 days")
        
        # Validate amount range
        if self.min_amount and self.max_amount:
            if self.max_amount < self.min_amount:
                raise ValueError("max_amount must be greater than min_amount")
        
        return self


class SaleListResponse(BaseSchema):
    """Response for sale list"""
    sales: List[SaleResponse]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)
    
    # Summary statistics
    total_revenue: Decimal = Field(..., ge=0)
    total_discount: Decimal = Field(..., ge=0)
    average_sale: Decimal = Field(..., ge=0)


# ============================================
# Reporting & Analytics
# ============================================

class SalesSummary(BaseSchema):
    """Comprehensive sales summary for reporting"""
    
    # Basic metrics
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    total_cost: Decimal = Field(..., ge=0)
    gross_profit: Decimal
    profit_margin: Decimal = Field(..., description="Percentage")
    average_sale: Decimal = Field(..., ge=0)
    
    # Discount breakdown
    total_discount: Decimal = Field(..., ge=0)
    contract_discount: Decimal = Field(..., ge=0, description="From contracts")
    manual_discount: Decimal = Field(..., ge=0, description="Additional manual")
    average_discount_rate: Decimal = Field(..., description="Percentage")
    
    # Tax
    total_tax: Decimal = Field(..., ge=0)
    
    # Payment methods
    cash_sales: int = Field(..., ge=0)
    cash_revenue: Decimal = Field(..., ge=0)
    card_sales: int = Field(..., ge=0)
    card_revenue: Decimal = Field(..., ge=0)
    mobile_money_sales: int = Field(..., ge=0)
    mobile_money_revenue: Decimal = Field(..., ge=0)
    insurance_sales: int = Field(default=0, ge=0)
    insurance_revenue: Decimal = Field(default=Decimal('0.00'), ge=0)
    
    # Insurance breakdown
    total_insurance_claims: int = Field(default=0, ge=0)
    total_copay_collected: Decimal = Field(default=Decimal('0.00'), ge=0)
    total_insurance_billed: Decimal = Field(default=Decimal('0.00'), ge=0)
    
    # Customer metrics
    unique_customers: int = Field(..., ge=0)
    new_customers: int = Field(..., ge=0)
    returning_customers: int = Field(..., ge=0)
    
    # Contract performance
    contracts_used: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Breakdown by contract"
    )
    
    # Period
    start_date: datetime
    end_date: datetime


class ContractPerformance(BaseSchema):
    """Performance metrics by price contract"""
    contract_id: uuid.UUID
    contract_name: str
    contract_code: str
    contract_type: str
    discount_percentage: Decimal
    
    # Usage metrics
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    total_discount_given: Decimal = Field(..., ge=0)
    average_sale: Decimal = Field(..., ge=0)
    average_discount: Decimal = Field(..., ge=0)
    
    # Customer metrics
    unique_customers: int = Field(..., ge=0)
    new_customers: int = Field(..., ge=0)
    customer_retention_rate: Decimal = Field(..., description="Percentage")
    
    # Effectiveness
    actual_discount_rate: Decimal = Field(..., description="Actual % given")
    revenue_impact: Decimal = Field(..., description="Revenue gained/lost")
    
    # Period
    start_date: datetime
    end_date: datetime


class TopSellingDrug(BaseSchema):
    """Top selling drug statistics"""
    drug_id: uuid.UUID
    drug_name: str
    drug_sku: Optional[str]
    drug_category: Optional[str]
    
    total_quantity: int = Field(..., gt=0)
    total_revenue: Decimal = Field(..., ge=0)
    total_cost: Decimal = Field(..., ge=0)
    gross_profit: Decimal
    profit_margin: Decimal
    
    sale_count: int = Field(..., gt=0)
    average_price: Decimal = Field(..., ge=0)
    average_quantity_per_sale: Decimal = Field(..., ge=0)
    
    # Contract breakdown
    contracts_used: List[Dict[str, Any]] = Field(default_factory=list)


class CashierPerformance(BaseSchema):
    """Cashier performance metrics"""
    cashier_id: uuid.UUID
    cashier_name: str
    cashier_role: str
    
    # Sales metrics
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    average_sale: Decimal = Field(..., ge=0)
    total_transactions: int = Field(..., ge=0)
    
    # Efficiency
    average_time_per_sale: Optional[int] = Field(None, description="Seconds")
    sales_per_hour: Decimal = Field(..., ge=0)
    
    # Quality metrics
    refunds_processed: int = Field(..., ge=0)
    refund_rate: Decimal = Field(..., description="Percentage")
    
    # Contract usage
    contracts_applied: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Period
    start_date: datetime
    end_date: datetime


class DailySalesSummary(BaseSchema):
    """Daily sales summary for trends"""
    date: date
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    total_transactions: int = Field(..., ge=0)
    average_transaction: Decimal = Field(..., ge=0)
    unique_customers: int = Field(..., ge=0)
    
    # Peak hour
    peak_hour: Optional[int] = Field(None, description="Hour with most sales")
    peak_hour_sales: int = Field(default=0, ge=0)


# ============================================
# Export & Bulk Operations
# ============================================

class ExportSalesRequest(BaseSchema):
    """Request to export sales data"""
    filters: SaleFilters
    format: str = Field(
        default='excel',
        pattern="^(excel|csv|pdf)$"
    )
    include_items: bool = Field(default=True)
    include_customer_details: bool = Field(default=False)


class BulkSaleAction(BaseSchema):
    """Bulk action on multiple sales"""
    sale_ids: List[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=100
    )
    action: str = Field(
        ...,
        pattern="^(email_receipt|export|mark_verified)$"
    )