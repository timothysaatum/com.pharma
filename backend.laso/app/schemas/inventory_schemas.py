"""
Inventory Schemas
Schemas for branch inventory and stock management
"""
from pydantic import Field, field_validator, model_validator, computed_field, ConfigDict
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
import uuid

from app.schemas.base_schemas import BaseSchema, Money, TimestampSchema, SyncSchema


class BranchInventoryBase(BaseSchema):
    """Base branch inventory fields"""
    quantity: int = Field(default=0, ge=0, description="Total available quantity")
    reserved_quantity: int = Field(default=0, ge=0, description="Reserved for pending orders")
    location: Optional[str] = Field(None, max_length=100, description="Shelf/bin location")
    selling_price: Optional[Money] = Field(None, ge=0, description="Branch-specific selling price")
    
    @field_validator('reserved_quantity')
    @classmethod
    def validate_reserved(cls, v: int, info) -> int:
        """Ensure reserved quantity doesn't exceed total"""
        if 'quantity' in info.data and v > info.data['quantity']:
            raise ValueError('Reserved quantity cannot exceed total quantity')
        return v


class BranchInventoryCreate(BranchInventoryBase):
    """Schema for creating branch inventory"""
    branch_id: uuid.UUID
    drug_id: uuid.UUID


class BranchInventoryUpdate(BaseSchema):
    """Schema for updating branch inventory"""
    quantity: Optional[int] = Field(None, ge=0)
    reserved_quantity: Optional[int] = Field(None, ge=0)
    location: Optional[str] = None
    selling_price: Optional[Money] = Field(None, ge=0)


class BranchInventoryResponse(BranchInventoryBase, TimestampSchema, SyncSchema):
    """Schema for branch inventory API responses"""
    id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    
    @computed_field
    @property
    def available_quantity(self) -> int:
        """Calculate available quantity (serialised in API responses)."""
        return max(0, self.quantity - self.reserved_quantity)
    
    model_config = ConfigDict(from_attributes=True)


class BranchInventoryWithDetails(BranchInventoryResponse):
    """Branch inventory with drug and branch details"""
    drug_name: str
    drug_sku: Optional[str]
    drug_type: str = Field(default="otc", description="Drug type: otc, prescription, controlled, herbal, supplement")
    requires_prescription: bool = Field(default=False, description="Whether this drug requires a valid prescription to sell")
    catalog_unit_price: Decimal
    drug_unit_price: Decimal
    effective_unit_price: Decimal
    # Joined from Drug.reorder_level so the frontend can render per-drug stock
    # status badges without issuing a separate GET /drugs/{id} call per row.
    drug_reorder_level: int
    branch_name: str
    branch_code: str


class DrugBatchBase(BaseSchema):
    """Base drug batch fields"""
    batch_number: str = Field(..., min_length=1, max_length=100, description="Manufacturer's batch/lot number")
    quantity: int = Field(..., gt=0, description="Initial quantity received")
    remaining_quantity: Optional[int] = Field(None, ge=0, description="Current remaining quantity; defaults to quantity when not supplied")
    manufacturing_date: Optional[date] = None
    expiry_date: date = Field(..., description="Expiration date")
    cost_price: Optional[Money] = Field(
        None,
        description="Cost/acquisition price"
    )
    selling_price: Optional[Money] = Field(
        None,
        description="Selling price"
    )
    supplier: Optional[str] = Field(None, max_length=255)
    
    @field_validator('remaining_quantity')
    @classmethod
    def validate_remaining(cls, v: int, info) -> int:
        """Ensure remaining quantity doesn't exceed initial"""
        if 'quantity' in info.data and v > info.data['quantity']:
            raise ValueError('Remaining quantity cannot exceed initial quantity')
        return v
    
    @field_validator('expiry_date')
    @classmethod
    def validate_expiry(cls, v: date) -> date:
        """Ensure expiry date is in the future"""
        if v < date.today():
            raise ValueError('Expiry date must be in the future')
        return v

    @model_validator(mode='after')
    def default_remaining_quantity(self) -> 'DrugBatchBase':
        """Default remaining_quantity to quantity when not supplied by the client."""
        if self.remaining_quantity is None:
            self.remaining_quantity = self.quantity
        return self


class DrugBatchCreate(DrugBatchBase):
    """Schema for creating a drug batch"""
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    purchase_order_id: Optional[uuid.UUID] = None


class DrugBatchUpdate(BaseSchema):
    """Schema for updating a drug batch"""
    remaining_quantity: Optional[int] = Field(None, ge=0)
    cost_price: Optional[Money] = Field(
        None,
        description="Cost/acquisition price"
    )
    selling_price: Optional[Money] = Field(
        None,
        description="Selling price"
    )
    supplier: Optional[str] = None


class DrugBatchResponse(DrugBatchBase, TimestampSchema, SyncSchema):
    """Schema for drug batch API responses"""
    id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    purchase_order_id: Optional[uuid.UUID] = None
    
    @computed_field
    @property
    def days_until_expiry(self) -> int:
        """Days remaining until batch expiry (serialised in API responses)."""
        return (self.expiry_date - date.today()).days
    
    @computed_field
    @property
    def is_expired(self) -> bool:
        """Whether the batch expiry date has passed (serialised in API responses)."""
        return self.expiry_date < date.today()
    
    @computed_field
    @property
    def is_expiring_soon(self) -> bool:
        """Whether the batch expires within 90 days (serialised in API responses)."""
        return 0 <= self.days_until_expiry <= 90
    
    model_config = ConfigDict(from_attributes=True)


class DrugBatchWithDetails(DrugBatchResponse):
    """Drug batch with drug details"""
    drug_name: str
    drug_generic_name: Optional[str]
    drug_sku: Optional[str]
    branch_name: str


class StockAdjustmentBase(BaseSchema):
    """Base stock adjustment fields"""
    adjustment_type: str = Field(
        ...,
        pattern="^(damage|expired|theft|return|correction|transfer)$",
        description="Type of adjustment"
    )
    quantity_change: int = Field(..., description="Positive for additions, negative for reductions")
    reason: Optional[str] = Field(None, description="Reason for adjustment")
    transfer_to_branch_id: Optional[uuid.UUID] = Field(None, description="For transfer type only")


class StockAdjustmentCreate(StockAdjustmentBase):
    """Schema for creating a stock adjustment"""
    branch_id: uuid.UUID
    drug_id: uuid.UUID


class StockAdjustmentResponse(StockAdjustmentBase, TimestampSchema):
    """Schema for stock adjustment API responses"""
    id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    previous_quantity: int
    new_quantity: int
    adjusted_by: uuid.UUID
    
    model_config = ConfigDict(from_attributes=True)


class StockAdjustmentWithDetails(StockAdjustmentResponse):
    """Stock adjustment with related details"""
    drug_name: str
    branch_name: str
    adjusted_by_name: str
    transfer_to_branch_name: Optional[str] = None


class StockTransferCreate(BaseSchema):
    """Schema for transferring stock between branches"""
    from_branch_id: uuid.UUID
    to_branch_id: uuid.UUID
    drug_id: uuid.UUID
    quantity: int = Field(..., gt=0)
    reason: str = Field(..., min_length=1, max_length=500)
    
    @field_validator('to_branch_id')
    @classmethod
    def validate_different_branches(cls, v: uuid.UUID, info) -> uuid.UUID:
        """Ensure source and destination are different"""
        if 'from_branch_id' in info.data and v == info.data['from_branch_id']:
            raise ValueError('Source and destination branches must be different')
        return v


class StockTransferResponse(BaseSchema):
    """Response for stock transfer"""
    source_adjustment: StockAdjustmentResponse
    destination_adjustment: StockAdjustmentResponse
    success: bool = True
    message: str = "Stock transferred successfully"


class InventoryValuationItem(BaseSchema):
    """Individual item in inventory valuation"""
    drug_id: uuid.UUID
    drug_name: str
    sku: Optional[str]
    quantity: int
    cost_price: Decimal
    selling_price: Decimal
    branch_selling_price: Optional[Decimal] = None
    total_cost_value: Decimal
    total_selling_value: Decimal
    potential_profit: Decimal


class InventoryValuationResponse(BaseSchema):
    """Complete inventory valuation report"""
    branch_id: uuid.UUID
    branch_name: str
    valuation_date: datetime
    items: List[InventoryValuationItem]
    total_items: int
    total_quantity: int
    total_cost_value: Decimal
    total_selling_value: Decimal
    total_potential_profit: Decimal
    profit_margin_percentage: Decimal


class LowStockItem(BaseSchema):
    """Item with low stock"""
    drug_id: uuid.UUID
    drug_name: str
    sku: Optional[str]
    branch_id: uuid.UUID
    branch_name: str
    quantity: int
    reorder_level: int
    reorder_quantity: int
    status: str  # out_of_stock, low_stock
    recommended_order_quantity: int


class LowStockReport(BaseSchema):
    """Low stock report"""
    organization_id: uuid.UUID
    branch_id: Optional[uuid.UUID] = None
    report_date: datetime
    items: List[LowStockItem]
    total_items: int
    out_of_stock_count: int
    low_stock_count: int


class ExpiringBatchItem(BaseSchema):
    """Item with expiring batch"""
    batch_id: uuid.UUID
    drug_id: uuid.UUID
    drug_name: str
    batch_number: str
    branch_id: uuid.UUID
    branch_name: str
    remaining_quantity: int
    expiry_date: date
    days_until_expiry: int
    cost_value: Decimal
    selling_value: Decimal


class ExpiringBatchReport(BaseSchema):
    """Expiring batches report"""
    organization_id: uuid.UUID
    branch_id: Optional[uuid.UUID] = None
    report_date: datetime
    days_threshold: int = 90
    items: List[ExpiringBatchItem]
    total_items: int
    total_quantity: int
    total_cost_value: Decimal
    total_selling_value: Decimal


class InventoryMovementSummary(BaseSchema):
    """Summary of inventory movements"""
    drug_id: uuid.UUID
    drug_name: str
    branch_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    opening_stock: int
    purchases: int
    sales: int
    adjustments: int
    transfers_in: int
    transfers_out: int
    closing_stock: int
    turnover_rate: Optional[Decimal] = None
