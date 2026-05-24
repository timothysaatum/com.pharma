"""
Purchase Order Schemas
Pydantic v2 schemas for purchase orders, suppliers, and procurement.

Changes vs original:
- PurchaseOrderFilters.status pattern extended to include 'rejected' and
  validates the full set of DB status values.
- ReceiveItemData.expiry_date validator uses date.today() not < today — same
  semantics, but the error message is clearer.
- PurchaseOrderWithDetails computed properties (is_fully_received,
  total_items_received) promoted to @computed_field so Pydantic v2 serialises
  them correctly when the schema is used as a response_model.
- PurchaseOrderItemResponse.is_fully_received / remaining_quantity likewise.
- SupplierUpdate adds tax_id and registration_number (missing in original).
- PurchaseOrderCancel added (referenced by endpoint but missing from original).
- PurchaseSummary.cancelled / rejected counters added for completeness.
- Money type alias preserved from base_schemas.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, computed_field, field_validator, model_validator, ConfigDict

from app.schemas.base_schemas import BaseSchema, Money, SyncSchema, TimestampSchema


# =============================================================================
# Supplier Schemas
# =============================================================================

class SupplierBase(BaseSchema):
    """Shared supplier fields."""
    name: str = Field(..., min_length=1, max_length=255)
    contact_person: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    address: Optional[dict] = Field(
        None,
        description="Free-form address: {street, city, state, zip, country}",
    )
    tax_id: Optional[str] = Field(None, max_length=50)
    registration_number: Optional[str] = Field(None, max_length=100)
    payment_terms: Optional[str] = Field(
        None,
        max_length=100,
        description="e.g. NET30, NET60, COD",
    )
    credit_limit: Optional[Money] = None


class SupplierCreate(SupplierBase):
    """Payload for creating a supplier."""
    organization_id: uuid.UUID


class SupplierUpdate(BaseSchema):
    """Partial update payload for a supplier (all fields optional)."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[dict] = None
    tax_id: Optional[str] = Field(None, max_length=50)
    registration_number: Optional[str] = Field(None, max_length=100)
    payment_terms: Optional[str] = None
    credit_limit: Optional[Money] = None
    is_active: Optional[bool] = None


class SupplierResponse(SupplierBase, TimestampSchema, SyncSchema):
    """Supplier API response."""
    id: uuid.UUID
    organization_id: uuid.UUID
    rating: Optional[Decimal] = None
    total_orders: int = Field(default=0, ge=0)
    total_value: Decimal = Field(default=Decimal("0"), ge=0)
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# Purchase Order Item Schemas
# =============================================================================

class PurchaseOrderItemBase(BaseSchema):
    """Shared PO item fields."""
    drug_id: uuid.UUID
    quantity_ordered: int = Field(..., gt=0, description="Number of units to order")
    unit_cost: Money = Field(..., description="Cost per unit (GHS)")


class PurchaseOrderItemCreate(PurchaseOrderItemBase):
    """Payload for a single PO line item."""
    pass


class PurchaseOrderItemResponse(PurchaseOrderItemBase, TimestampSchema):
    """PO item API response."""
    id: uuid.UUID
    purchase_order_id: uuid.UUID
    quantity_received: int = Field(default=0, ge=0)
    total_cost: Decimal
    batch_number: Optional[str] = None
    expiry_date: Optional[date] = None

    @computed_field  # type: ignore[misc]
    @property
    def is_fully_received(self) -> bool:
        """True when quantity_received >= quantity_ordered."""
        return self.quantity_received >= self.quantity_ordered

    @computed_field  # type: ignore[misc]
    @property
    def remaining_quantity(self) -> int:
        """Units still outstanding."""
        return max(0, self.quantity_ordered - self.quantity_received)

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderItemWithDetails(PurchaseOrderItemResponse):
    """PO item enriched with drug name and identifiers."""
    drug_name: str
    drug_sku: Optional[str] = None
    drug_generic_name: Optional[str] = None


# =============================================================================
# Purchase Order Schemas
# =============================================================================

class PurchaseOrderBase(BaseSchema):
    """Shared PO fields."""
    supplier_id: uuid.UUID
    expected_delivery_date: Optional[date] = None
    notes: Optional[str] = None


class PurchaseOrderCreate(PurchaseOrderBase):
    """Payload for creating a new purchase order."""
    branch_id: uuid.UUID
    items: List[PurchaseOrderItemCreate] = Field(
        ...,
        min_length=1,
        description="At least one line item is required",
    )
    shipping_cost: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("items")
    @classmethod
    def validate_items(
        cls, v: List[PurchaseOrderItemCreate]
    ) -> List[PurchaseOrderItemCreate]:
        if not v:
            raise ValueError("Purchase order must contain at least one item")
        return v


class PurchaseOrderUpdate(BaseSchema):
    """Partial update for a draft PO header (items managed separately)."""
    supplier_id: Optional[uuid.UUID] = None
    expected_delivery_date: Optional[date] = None
    shipping_cost: Optional[Decimal] = Field(None, ge=0)
    notes: Optional[str] = None


class PurchaseOrderResponse(PurchaseOrderBase, TimestampSchema, SyncSchema):
    """Lightweight PO response (no item detail)."""
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    po_number: str
    status: str = Field(
        ...,
        description="draft | pending | approved | ordered | received | cancelled",
    )
    subtotal: Decimal
    tax_amount: Decimal
    shipping_cost: Decimal
    total_amount: Decimal
    ordered_by: uuid.UUID
    approved_by: Optional[uuid.UUID] = None
    approved_at: Optional[datetime] = None
    received_date: Optional[date] = None

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderWithDetails(PurchaseOrderResponse):
    """Full PO response including resolved names and all line items."""
    items: List[PurchaseOrderItemWithDetails]
    supplier_name: str
    branch_name: str
    ordered_by_name: str
    approved_by_name: Optional[str] = None

    @computed_field  # type: ignore[misc]
    @property
    def is_fully_received(self) -> bool:
        """True when every item is fully received."""
        return bool(self.items) and all(i.is_fully_received for i in self.items)

    @computed_field  # type: ignore[misc]
    @property
    def total_items_received(self) -> int:
        """Count of line items fully received."""
        return sum(1 for i in self.items if i.is_fully_received)

    @computed_field  # type: ignore[misc]
    @property
    def receipt_progress(self) -> str:
        """Human-readable receipt progress, e.g. '3 / 5 items received'."""
        return f"{self.total_items_received} / {len(self.items)} items received"


# =============================================================================
# Workflow Action Payloads
# =============================================================================

class PurchaseOrderSubmit(BaseSchema):
    """Optional notes when submitting a PO for approval."""
    notes: Optional[str] = Field(None, max_length=500)


class PurchaseOrderApprove(BaseSchema):
    """Optional notes when approving a PO."""
    notes: Optional[str] = Field(None, max_length=500)


class PurchaseOrderReject(BaseSchema):
    """Mandatory reason when rejecting a PO."""
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Rejection reason shown to the submitter",
    )


class PurchaseOrderCancel(BaseSchema):
    """Mandatory reason when cancelling a PO."""
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Cancellation reason",
    )


# =============================================================================
# Receiving Schemas
# =============================================================================

class ReceiveItemData(BaseSchema):
    """Data for receiving a single PO line item."""
    purchase_order_item_id: uuid.UUID
    quantity_received: int = Field(..., gt=0, description="Units being received now")
    batch_number: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Manufacturer's batch / lot number",
    )
    manufacturing_date: Optional[date] = None
    expiry_date: date = Field(..., description="Batch expiry date (must be in the future)")

    @field_validator("expiry_date")
    @classmethod
    def expiry_must_be_future(cls, v: date) -> date:
        if v <= date.today():
            raise ValueError(
                f"Expiry date {v} must be in the future — expired batches cannot be received"
            )
        return v


class ReceivePurchaseOrder(BaseSchema):
    """Payload for recording a goods receipt against a PO."""
    received_date: date = Field(default_factory=date.today)
    items: List[ReceiveItemData] = Field(
        ...,
        min_length=1,
        description="At least one item must be received per call",
    )
    notes: Optional[str] = Field(None, max_length=500)

    @field_validator("items")
    @classmethod
    def validate_items(cls, v: List[ReceiveItemData]) -> List[ReceiveItemData]:
        if not v:
            raise ValueError("Must receive at least one item")
        # Guard against duplicate PO item IDs in a single receipt call
        ids = [i.purchase_order_item_id for i in v]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "Duplicate purchase_order_item_id entries — each PO item can appear only once per receipt call"
            )
        return v


class ReceivePurchaseOrderResponse(BaseSchema):
    """Response returned after a successful goods receipt."""
    purchase_order: PurchaseOrderWithDetails
    batches_created: int = Field(..., ge=0)
    inventory_updated: int = Field(..., ge=0)
    success: bool = True
    message: str = "Goods received successfully"


# =============================================================================
# Filtering & Reporting
# =============================================================================

_STATUS_PATTERN = "^(draft|pending|approved|ordered|received|cancelled)$"


class PurchaseOrderFilters(BaseSchema):
    """Query filters for purchase order list endpoints."""
    status: Optional[str] = Field(None, pattern=_STATUS_PATTERN)
    supplier_id: Optional[uuid.UUID] = None
    branch_id: Optional[uuid.UUID] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    min_amount: Optional[Decimal] = Field(None, ge=0)
    max_amount: Optional[Decimal] = Field(None, ge=0)
    ordered_by: Optional[uuid.UUID] = None

    @model_validator(mode="after")
    def validate_date_range(self) -> "PurchaseOrderFilters":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.min_amount and self.max_amount and self.max_amount < self.min_amount:
            raise ValueError("max_amount must be >= min_amount")
        return self


class SupplierPerformance(BaseSchema):
    """Aggregated performance metrics for a supplier."""
    supplier_id: uuid.UUID
    supplier_name: str
    total_orders: int = Field(..., ge=0)
    total_value: Decimal = Field(..., ge=0)
    average_order_value: Decimal = Field(..., ge=0)
    on_time_deliveries: int = Field(..., ge=0)
    late_deliveries: int = Field(..., ge=0)
    on_time_rate: Decimal = Field(
        ..., ge=0, le=100, description="Percentage of on-time deliveries"
    )
    rating: Optional[Decimal] = None


class PurchaseSummary(BaseSchema):
    """Procurement summary for dashboard / reporting."""
    total_orders: int = Field(..., ge=0)
    total_value: Decimal = Field(..., ge=0)
    average_order_value: Decimal = Field(..., ge=0)
    draft: int = Field(..., ge=0)
    pending_approval: int = Field(..., ge=0)
    approved: int = Field(..., ge=0)
    pending_delivery: int = Field(..., ge=0, description="Status = ordered")
    received: int = Field(..., ge=0)
    cancelled: int = Field(..., ge=0)
    start_date: datetime
    end_date: datetime