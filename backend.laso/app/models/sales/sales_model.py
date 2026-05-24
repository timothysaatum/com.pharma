from app.db.base import Base
from sqlalchemy import (
    String, Integer, Boolean, DateTime, Numeric, Text,
    ForeignKey, Index, CheckConstraint, Date
)
from app.models.db_types import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, date
import uuid

from app.models.core.mixins import SoftDeleteMixin, SoftDeleteMixin, SyncTrackingMixin, TimestampMixin
from app.models.pricing.pricing_model import PriceContract
from app.schemas.base_schemas import Money
if TYPE_CHECKING:
    from app.models.customer.customer_model import Customer
    from app.models.pharmacy.pharmacy_model import Branch 

class Sale(Base, TimestampMixin, SyncTrackingMixin):
    """
    Sales transactions with comprehensive tracking.
    Optimized for offline-first with conflict resolution.
    
    PRICING STRATEGY:
    - Discounts come ONLY from PriceContract applied to the sale
    - No additional manual discounts allowed
    - Personnel can select appropriate contract at checkout
    """
    __tablename__ = 'sales'
    
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
    
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('branches.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    # Unique sale identifier
    sale_number: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
        comment="Human-readable sale number like BR001-20260112-0001"
    )
    
    # Customer information
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('customers.id', ondelete='SET NULL'),
        nullable=True,
        index=True
    )
    
    customer_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="For walk-in customers without registration"
    )
    
    # ==================== FINANCIAL DETAILS ====================
    
    subtotal: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="Sum of all items at base prices (before contract discount and tax)"
    )
    
    discount_amount: Mapped[float] = mapped_column(
        Numeric(10, 2),
        default=0,
        nullable=False,
        comment="Total discount from applied contract only"
    )
    
    tax_amount: Mapped[float] = mapped_column(
        Numeric(10, 2),
        default=0,
        nullable=False,
        comment="Total tax charged"
    )
    
    total_amount: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        index=True,
        comment="Final amount: (subtotal - discount_amount + tax_amount)"
    )
    
    # ==================== PRICING CONTRACT ====================
    
    price_contract_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('price_contracts.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Price contract applied to this entire sale (selected by cashier)"
    )
    
    contract_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="Snapshot of contract name for historical records"
    )
    
    contract_discount_percentage: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        comment="Snapshot of discount percentage from contract"
    )
    
    # ==================== PAYMENT DETAILS ====================
    
    payment_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="cash, card, mobile_money, insurance, credit"
    )
    
    payment_status: Mapped[str] = mapped_column(
        String(50),
        default='completed',
        nullable=False,
        index=True,
        comment="pending, completed, partial, refunded, cancelled"
    )
    
    amount_paid: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Actual amount paid by customer"
    )
    
    change_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Change given to customer"
    )
    
    # Payment reference (for card/mobile money)
    payment_reference: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="Transaction ID from payment gateway"
    )
    
    # ==================== PRESCRIPTION INFORMATION ====================
    
    prescription_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('prescriptions.id', ondelete='SET NULL'),
        nullable=True
    )
    
    prescription_number: Mapped[Optional[str]] = mapped_column(String(100))
    prescriber_name: Mapped[Optional[str]] = mapped_column(String(255))
    prescriber_license: Mapped[Optional[str]] = mapped_column(String(100))
    
    # ==================== STAFF TRACKING ====================
    
    cashier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
        comment="User who processed the sale"
    )
    
    pharmacist_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=True,
        comment="Pharmacist who verified prescription"
    )
    
    # ==================== INSURANCE-SPECIFIC FIELDS ====================
    
    insurance_claim_number: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="Insurance claim reference number"
    )
    
    patient_copay_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Amount patient paid (copay)"
    )
    
    insurance_covered_amount: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        comment="Amount covered by insurance"
    )
    
    insurance_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether insurance has been verified"
    )
    
    insurance_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="When insurance verification was completed"
    )
    
    insurance_verified_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        comment="User who verified insurance"
    )
    
    # ==================== ADDITIONAL INFORMATION ====================
    
    notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Sale status
    status: Mapped[str] = mapped_column(
        String(50),
        default='completed',
        nullable=False,
        index=True,
        comment="draft, completed, cancelled, refunded"
    )
    
    # ==================== CANCELLATION/REFUND TRACKING ====================
    
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL')
    )
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text)
    
    refund_amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # ==================== RECEIPT TRACKING ====================
    
    receipt_printed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    receipt_emailed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    # ==================== RELATIONSHIPS ====================
    
    price_contract: Mapped[Optional["PriceContract"]] = relationship()
    branch: Mapped["Branch"] = relationship(back_populates="sales")
    customer: Mapped[Optional["Customer"]] = relationship(back_populates="sales")
    items: Mapped[List["SaleItem"]] = relationship(
        back_populates="sale",
        cascade="all, delete-orphan"
    )
    
    # ==================== PROPERTIES ====================
    
    @property
    def is_insurance_verified(self) -> bool:
        """Check if insurance was verified for this sale"""
        return self.insurance_verified and self.insurance_verified_at is not None
    
    @property
    def has_contract_discount(self) -> bool:
        """Check if a pricing contract was applied"""
        return self.price_contract_id is not None and self.discount_amount > 0
    
    def calculate_totals(self):
        """
        Recalculate sale totals based on items.
        Call this after adding/removing items or changing contract.
        """
        if not self.items:
            self.subtotal = 0
            self.discount_amount = 0
            self.tax_amount = 0
            self.total_amount = 0
            return
        
        self.subtotal = sum(item.subtotal for item in self.items)
        self.discount_amount = sum(item.discount_amount for item in self.items)
        self.tax_amount = sum(item.tax_amount for item in self.items)
        self.total_amount = sum(item.total_price for item in self.items)
    
    # ==================== TABLE CONSTRAINTS ====================
    
    __table_args__ = (
        CheckConstraint(
            "payment_method IN ('cash', 'card', 'mobile_money', 'insurance', 'credit', 'split')",
            name='check_payment_method'
        ),
        CheckConstraint(
            "payment_status IN ('pending', 'completed', 'partial', 'refunded', 'cancelled')",
            name='check_payment_status'
        ),
        CheckConstraint(
            "status IN ('draft', 'completed', 'cancelled', 'refunded')",
            name='check_sale_status'
        ),
        CheckConstraint("subtotal >= 0", name='check_subtotal'),
        CheckConstraint("discount_amount >= 0", name='check_discount'),
        CheckConstraint("discount_amount <= subtotal", name='check_discount_not_exceed_subtotal'),
        CheckConstraint("tax_amount >= 0", name='check_tax'),
        CheckConstraint("total_amount >= 0", name='check_total'),
        
        # Indexes
        Index('idx_sale_org', 'organization_id'),
        Index('idx_sale_branch', 'branch_id'),
        Index('idx_sale_customer', 'customer_id'),
        Index('idx_sale_number', 'sale_number'),
        Index('idx_sale_date', 'created_at'),
        Index('idx_sale_status', 'status'),
        Index('idx_sale_payment', 'payment_method', 'payment_status'),
        Index('idx_sale_cashier', 'cashier_id'),
        Index('idx_sale_contract', 'price_contract_id'),
        Index('idx_sale_sync', 'sync_status', 'sync_version'),
        
        # Composite indexes for common queries
        Index('idx_sale_branch_date', 'branch_id', 'created_at'),
        Index('idx_sale_org_date', 'organization_id', 'created_at'),
    )


class SaleItem(Base, TimestampMixin):
    """
    Individual line items in a sale.
    
    PRICING FLOW:
    1. Start with drug's base unit_price
    2. Apply sale's price_contract discount (if any)
    3. Calculate tax on discounted price
    4. Store final total_price
    """
    __tablename__ = 'sale_items'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    sale_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('sales.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drugs.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    
    # Snapshot drug information for historical accuracy
    drug_name: Mapped[str] = mapped_column(String(255), nullable=False)
    drug_sku: Mapped[Optional[str]] = mapped_column(String(100))
    
    # Batch tracking
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drug_batches.id', ondelete='SET NULL'),
        nullable=True,
        comment="Track which batch this item was sold from (FIFO/FEFO)"
    )
    
    # ==================== PRICING BREAKDOWN ====================
    
    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Number of units sold"
    )
    
    unit_price: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="Base unit price from drug catalog (before discount)"
    )
    
    subtotal: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="quantity * unit_price (before discount)"
    )
    
    # Contract-based discount
    discount_percentage: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0,
        nullable=False,
        comment="Discount percentage applied from sale's price contract"
    )
    
    discount_amount: Mapped[float] = mapped_column(
        Numeric(10, 2),
        default=0,
        nullable=False,
        comment="Total discount: (subtotal * discount_percentage / 100)"
    )
    
    # Tax calculation
    tax_rate: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0,
        nullable=False,
        comment="Tax rate percentage from drug catalog"
    )
    
    tax_amount: Mapped[float] = mapped_column(
        Numeric(10, 2),
        default=0,
        nullable=False,
        comment="Tax on discounted price: (subtotal - discount_amount) * tax_rate / 100"
    )
    
    # Final price
    total_price: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="Final: (subtotal - discount_amount + tax_amount)"
    )
    
    # ==================== PRESCRIPTION TRACKING ====================
    
    requires_prescription: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    prescription_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    # ==================== RELATIONSHIPS ====================
    
    sale: Mapped["Sale"] = relationship(back_populates="items")
    
    # ==================== METHODS ====================
    
    def apply_pricing(self, contract: Optional[PriceContract] = None):
        """
        Calculate pricing for this item.
        
        Args:
            contract: Optional PriceContract to apply discount from
        """
        # Calculate subtotal
        self.subtotal = float(self.quantity * self.unit_price)
        
        # Apply contract discount if provided
        if contract:
            self.discount_percentage = float(contract.discount_percentage)
            self.discount_amount = round(
                self.subtotal * (self.discount_percentage / 100), 
                2
            )
        else:
            self.discount_percentage = 0
            self.discount_amount = 0
        
        # Calculate tax on discounted price
        discounted_price = self.subtotal - self.discount_amount
        self.tax_amount = round(
            discounted_price * (self.tax_rate / 100),
            2
        )
        
        # Calculate final total
        self.total_price = round(
            self.subtotal - self.discount_amount + self.tax_amount,
            2
        )
    
    # ==================== TABLE CONSTRAINTS ====================
    
    __table_args__ = (
        CheckConstraint("quantity > 0", name='check_sale_item_quantity'),
        CheckConstraint("unit_price >= 0", name='check_sale_item_unit_price'),
        CheckConstraint("subtotal >= 0", name='check_sale_item_subtotal'),
        CheckConstraint("discount_amount >= 0", name='check_sale_item_discount'),
        CheckConstraint("discount_amount <= subtotal", name='check_sale_item_discount_not_exceed'),
        CheckConstraint("tax_amount >= 0", name='check_sale_item_tax'),
        CheckConstraint("total_price >= 0", name='check_sale_item_total'),
        
        # Indexes
        Index('idx_sale_item_sale', 'sale_id'),
        Index('idx_sale_item_drug', 'drug_id'),
        Index('idx_sale_item_date', 'created_at'),
        Index('idx_sale_item_batch', 'batch_id'),
    )


# ==================== SUPPLIER AND PURCHASE ORDER MODELS ====================

class Supplier(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """Supplier/vendor management"""
    __tablename__ = 'suppliers'
    
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
    
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    
    contact_person: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    
    address: Mapped[Optional[dict]] = mapped_column(JSONB)
    
    # Business information
    tax_id: Mapped[Optional[str]] = mapped_column(String(50))
    registration_number: Mapped[Optional[str]] = mapped_column(String(100))
    
    # Terms
    payment_terms: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="NET30, NET60, COD, etc."
    )
    
    credit_limit: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    
    # Performance tracking
    rating: Mapped[Optional[float]] = mapped_column(
        Numeric(3, 2),
        comment="Supplier rating 0.00-5.00"
    )
    
    total_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    
    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Relationships
    purchase_orders: Mapped[List["PurchaseOrder"]] = relationship(back_populates="supplier")
    
    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", 
                       name='check_supplier_rating'),
        Index('idx_supplier_org', 'organization_id'),
        Index('idx_supplier_active', 'is_active'),
    )


class PurchaseOrder(Base, TimestampMixin, SyncTrackingMixin):
    """Purchase orders for inventory replenishment"""
    __tablename__ = 'purchase_orders'
    
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
    
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('branches.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    po_number: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True
    )
    
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('suppliers.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    
    # Financial details
    subtotal: Mapped[Money] = mapped_column(Numeric(12, 2), nullable=False)
    tax_amount: Mapped[Money] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    shipping_cost: Mapped[Money] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    total_amount: Mapped[Money] = mapped_column(Numeric(12, 2), nullable=False)
    
    # Status tracking
    status: Mapped[str] = mapped_column(
        String(50),
        default='draft',
        nullable=False,
        index=True,
        comment="draft, pending, approved, ordered, received, cancelled"
    )
    
    # Approval workflow
    ordered_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False
    )
    
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL')
    )
    
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # Dates
    expected_delivery_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    received_date: Mapped[Optional[date]] = mapped_column(Date)
    
    notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Relationships
    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders")
    items: Mapped[List["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan"
    )
    
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'ordered', 'received', 'cancelled')",
            name='check_po_status'
        ),
        Index('idx_po_org', 'organization_id'),
        Index('idx_po_branch', 'branch_id'),
        Index('idx_po_supplier', 'supplier_id'),
        Index('idx_po_status', 'status'),
        Index('idx_po_expected_delivery', 'expected_delivery_date'),
    )


class PurchaseOrderItem(Base, TimestampMixin):
    """Line items in purchase orders"""
    __tablename__ = 'purchase_order_items'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('purchase_orders.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drugs.id', ondelete='RESTRICT'),
        nullable=False
    )
    
    quantity_ordered: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    unit_cost: Mapped[Money] = mapped_column(Numeric(10, 2), nullable=False)
    total_cost: Mapped[Money] = mapped_column(Numeric(12, 2), nullable=False)
    
    # Batch information (filled upon receipt)
    batch_number: Mapped[Optional[str]] = mapped_column(String(100))
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    
    # Relationships
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")
    
    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name='check_po_item_quantity'),
        CheckConstraint("quantity_received >= 0", name='check_po_item_received'),
        CheckConstraint("quantity_received <= quantity_ordered", name='check_po_item_received_max'),
        Index('idx_po_item_po', 'purchase_order_id'),
        Index('idx_po_item_drug', 'drug_id'),
    )