from app.schemas.base_schemas import BaseSchema
from pydantic import (
    Field, field_validator, 
    model_validator, computed_field
)
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timezone
from decimal import Decimal
import uuid

from app.schemas.drugs_schemas import DrugCategoryResponse, DrugResponse
from app.schemas.inventory_schemas import BranchInventoryResponse



class SyncOperation(BaseSchema):
    """Single sync operation"""
    operation: str = Field(..., pattern="^(create|update|delete)$")
    table_name: str = Field(..., max_length=100)
    record_id: str = Field(..., max_length=100)
    data: Dict[str, Any]
    timestamp: datetime


class BulkSyncRequest(BaseSchema):
    """Batch sync request from offline client"""
    operations: List[SyncOperation] = Field(..., max_length=1000)

    @field_validator('operations')
    @classmethod
    def validate_operations(cls, v: List[SyncOperation]) -> List[SyncOperation]:
        """Limit bulk operations"""
        if len(v) > 1000:
            raise ValueError("Cannot sync more than 1000 operations at once")
        return v


class SyncConflict(BaseSchema):
    """Sync conflict details"""
    table_name: str
    record_id: str
    local_version: int
    server_version: int
    local_data: Dict[str, Any]
    server_data: Dict[str, Any]
    resolution: Optional[str] = Field(
        None,
        pattern="^(server_wins|local_wins|merged|manual_required)$"
    )


class BulkSyncResponse(BaseSchema):
    """Response to bulk sync"""
    processed: int = Field(..., ge=0)
    succeeded: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    conflicts: List[SyncConflict] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    sync_timestamp: datetime


class IncrementalSyncRequest(BaseSchema):
    """Request for incremental sync"""
    last_sync_timestamp: Optional[datetime] = None
    tables: List[str] = Field(
        default=["drugs", "branch_inventory"],
        description="Tables to sync"
    )
    branch_id: Optional[uuid.UUID] = Field(
        None,
        description="Filter data for specific branch"
    )


class IncrementalSyncResponse(BaseSchema):
    """Incremental sync response"""
    drugs: List[DrugResponse] = Field(default_factory=list)
    inventory: List[BranchInventoryResponse] = Field(default_factory=list)
    categories: List[DrugCategoryResponse] = Field(default_factory=list)
    sync_timestamp: datetime
    has_more: bool = Field(default=False)
    next_page: Optional[int] = None


# ============================================
# Pagination & Filtering Schemas
# ============================================

class PaginationParams(BaseSchema):
    """Common pagination parameters"""
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=50, ge=1, le=500)

    @computed_field
    @property
    def skip(self) -> int:
        """Calculate skip value for database queries"""
        return (self.page - 1) * self.page_size

    @computed_field
    @property
    def limit(self) -> int:
        """Get limit value"""
        return self.page_size


class DrugFilters(BaseSchema):
    """Filters for drug queries"""
    search: Optional[str] = Field(None, max_length=255)
    category_id: Optional[uuid.UUID] = None
    drug_type: Optional[str] = Field(
        None,
        pattern="^(prescription|otc|controlled|herbal|supplement)$"
    )
    manufacturer: Optional[str] = Field(None, max_length=255)
    is_active: bool = True
    min_price: Optional[Decimal] = Field(None, ge=0)
    max_price: Optional[Decimal] = Field(None, ge=0)
    low_stock: bool = Field(
        default=False,
        description="Filter drugs below reorder level"
    )
    requires_prescription: Optional[bool] = None


class SaleFilters(BaseSchema):
    """Filters for sale queries"""
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[str] = Field(
        None,
        pattern="^(draft|completed|cancelled|refunded)$"
    )
    payment_method: Optional[str] = Field(
        None,
        pattern="^(cash|card|mobile_money|insurance|credit|split)$"
    )
    cashier_id: Optional[uuid.UUID] = None
    customer_id: Optional[uuid.UUID] = None
    min_amount: Optional[Decimal] = Field(None, ge=0)
    max_amount: Optional[Decimal] = Field(None, ge=0)

    @model_validator(mode='after')
    def validate_date_range(self) -> 'SaleFilters':
        """Validate date range"""
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("End date must be after start date")
            
            # Prevent too large date ranges (performance)
            delta = self.end_date - self.start_date
            if delta.days > 365:
                raise ValueError("Date range cannot exceed 365 days")
        
        return self


# ============================================
# Analytics & Reporting Schemas
# ============================================

class SalesSummary(BaseSchema):
    """Sales summary for reporting"""
    total_sales: int = Field(..., ge=0)
    total_revenue: Decimal = Field(..., ge=0)
    average_sale: Decimal = Field(..., ge=0)
    total_discount: Decimal = Field(..., ge=0)
    total_tax: Decimal = Field(..., ge=0)
    start_date: datetime
    end_date: datetime


class TopSellingDrug(BaseSchema):
    """Top selling drug statistics"""
    drug_id: uuid.UUID
    drug_name: str
    total_quantity: int = Field(..., gt=0)
    total_revenue: Decimal = Field(..., ge=0)
    sale_count: int = Field(..., gt=0)
    average_price: Decimal = Field(..., ge=0)


class InventoryValuation(BaseSchema):
    """Inventory value report"""
    total_items: int = Field(..., ge=0)
    total_quantity: int = Field(..., ge=0)
    total_cost_value: Decimal = Field(..., ge=0)
    total_retail_value: Decimal = Field(..., ge=0)
    potential_profit: Decimal = Field(..., ge=0)
    branch_id: Optional[uuid.UUID] = None

    @computed_field
    @property
    def profit_margin_percentage(self) -> Decimal:
        """Calculate overall profit margin"""
        if self.total_cost_value > 0:
            margin = ((self.total_retail_value - self.total_cost_value) / 
                     self.total_cost_value) * 100
            return round(margin, 2)
        return Decimal("0")


class LowStockAlert(BaseSchema):
    """Low stock alert item"""
    drug_id: uuid.UUID
    drug_name: str
    current_quantity: int = Field(..., ge=0)
    reorder_level: int = Field(..., ge=0)
    reorder_quantity: int = Field(..., gt=0)
    branch_id: uuid.UUID
    branch_name: str


class ExpiringBatch(BaseSchema):
    """Expiring batch alert"""
    batch_id: uuid.UUID
    drug_id: uuid.UUID
    drug_name: str
    batch_number: str
    remaining_quantity: int = Field(..., gt=0)
    expiry_date: date
    days_until_expiry: int = Field(..., ge=0)
    branch_id: uuid.UUID


# ============================================
# Error Response Schemas
# ============================================

class ErrorDetail(BaseSchema):
    """Detailed error information"""
    field: Optional[str] = None
    message: str
    type: str
    context: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseSchema):
    """Standard error response"""
    error: str = Field(..., description="Error type/code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[List[ErrorDetail]] = Field(
        None,
        description="Detailed error information"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    request_id: Optional[str] = Field(
        None,
        description="Request ID for tracking"
    )


# ============================================
# Health Check Schema
# ============================================

class HealthCheckResponse(BaseSchema):
    """System health check"""
    status: str = Field(..., pattern="^(healthy|degraded|unhealthy)$")
    version: str
    database: str = Field(..., pattern="^(connected|disconnected)$")
    cache: Optional[str] = Field(None, pattern="^(connected|disconnected)$")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checks: Dict[str, bool] = Field(default_factory=dict)