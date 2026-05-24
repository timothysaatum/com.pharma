"""
Drug Schemas
Complete schemas for drug/product management
"""
from pydantic import Field, field_validator, computed_field, ConfigDict
from typing import Optional, List
from decimal import Decimal
import uuid
import re

from app.schemas.base_schemas import (
    BaseSchema, TimestampSchema, SyncSchema, Money
)


class DrugBase(BaseSchema):
    """Base drug fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Brand or trade name")
    generic_name: Optional[str] = Field(None, max_length=255, description="Generic/scientific name")
    brand_name: Optional[str] = Field(None, max_length=255)
    sku: Optional[str] = Field(None, max_length=100, description="Stock Keeping Unit")
    barcode: Optional[str] = Field(None, max_length=100, description="EAN, UPC, or other barcode")
    category_id: Optional[uuid.UUID] = None
    drug_type: str = Field(
        default="otc",
        pattern="^(prescription|otc|controlled|herbal|supplement)$",
        description="Type of drug"
    )
    dosage_form: Optional[str] = Field(None, max_length=100, description="tablet, capsule, syrup, etc.")
    strength: Optional[str] = Field(None, max_length=100, description="e.g., 500mg, 10mg/ml")
    manufacturer: Optional[str] = Field(None, max_length=255)
    supplier: Optional[str] = Field(None, max_length=255)
    ndc_code: Optional[str] = Field(None, max_length=50, description="National Drug Code")
    requires_prescription: bool = Field(default=False)
    controlled_substance_schedule: Optional[str] = Field(
        None, 
        max_length=10,
        description="DEA Schedule I-V for controlled substances"
    )
    unit_price: Money = Field(..., description="Selling price per unit")
    cost_price: Optional[Money] = Field(None, description="Cost/acquisition price")
    markup_percentage: Optional[Money] = Field(None,description="Markup percentage over cost price")
    tax_rate: Money = Field(default=0, description="Tax rate as percentage")
    reorder_level: int = Field(default=10, ge=0, description="Trigger reorder when stock falls below")
    reorder_quantity: int = Field(default=50, ge=1, description="Suggested reorder quantity")
    max_stock_level: Optional[int] = Field(None, ge=0, description="Maximum stock to maintain")
    unit_of_measure: str = Field(
        default="unit",
        max_length=50,
        description="unit, box, bottle, strip, etc."
    )
    description: Optional[str] = None
    usage_instructions: Optional[str] = None
    side_effects: Optional[str] = None
    contraindications: Optional[str] = None
    storage_conditions: Optional[str] = None
    image_url: Optional[str] = None
    is_active: bool = Field(default=True)
    
    @field_validator('sku', 'barcode')
    @classmethod
    def validate_alphanumeric(cls, v: Optional[str]) -> Optional[str]:
        """Validate SKU and barcode are alphanumeric"""
        if v and not re.match(r'^[A-Za-z0-9\-_]+$', v):
            raise ValueError('Must contain only letters, numbers, hyphens, and underscores')
        return v
    
    @field_validator('unit_price', 'cost_price')
    @classmethod
    def validate_price(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        """Validate prices are reasonable"""
        if v and v > 1000000:
            raise ValueError('Price exceeds maximum allowed value')
        return v


class DrugCreate(DrugBase):
    """Schema for creating a drug"""
    organization_id: uuid.UUID


class DrugUpdate(BaseSchema):
    """Schema for updating a drug (all fields optional)"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    generic_name: Optional[str] = None
    brand_name: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_id: Optional[uuid.UUID] = None
    drug_type: Optional[str] = Field(
        None,
        pattern="^(prescription|otc|controlled|herbal|supplement)$"
    )
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    manufacturer: Optional[str] = None
    supplier: Optional[str] = None
    ndc_code: Optional[str] = None
    requires_prescription: Optional[bool] = None
    controlled_substance_schedule: Optional[str] = None
    unit_price: Optional[Money] = Field(None, ge=0)
    cost_price: Optional[Money] = Field(None, ge=0)
    markup_percentage: Optional[Money] = Field(None, ge=0)
    tax_rate: Optional[Money] = Field(None, ge=0, le=100)
    reorder_level: Optional[int] = Field(None, ge=0)
    reorder_quantity: Optional[int] = Field(None, ge=1)
    max_stock_level: Optional[int] = None
    unit_of_measure: Optional[str] = None
    description: Optional[str] = None
    usage_instructions: Optional[str] = None
    side_effects: Optional[str] = None
    contraindications: Optional[str] = None
    storage_conditions: Optional[str] = None
    image_url: Optional[str] = None
    is_active: Optional[bool] = None


class DrugResponse(DrugBase, TimestampSchema, SyncSchema):
    """Schema for drug API responses"""
    id: uuid.UUID
    organization_id: uuid.UUID
    
    @computed_field
    @property
    def profit_margin(self) -> Optional[float]:
        """Calculate profit margin if cost price is available"""
        if self.cost_price and self.cost_price > 0:
            return float(((self.unit_price - self.cost_price) / self.cost_price) * 100)
        return None
    
    model_config = ConfigDict(from_attributes=True)


class DrugWithInventory(DrugResponse):
    """Drug response with inventory information"""
    total_quantity: int = 0
    available_quantity: int = 0
    reserved_quantity: int = 0
    inventory_status: str = "unknown"  # in_stock, low_stock, out_of_stock
    
    @computed_field
    @property
    def needs_reorder(self) -> bool:
        """Check if drug needs reordering"""
        return self.total_quantity <= self.reorder_level


class DrugCategoryBase(BaseSchema):
    """Base drug category fields"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None


class DrugCategoryCreate(DrugCategoryBase):
    """Schema for creating a drug category"""
    organization_id: uuid.UUID


class DrugCategoryUpdate(BaseSchema):
    """Schema for updating a drug category"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None


class DrugCategoryResponse(DrugCategoryBase, TimestampSchema, SyncSchema):
    """Schema for drug category API responses"""
    id: uuid.UUID
    organization_id: uuid.UUID
    path: Optional[str] = None
    level: int
    
    model_config = ConfigDict(from_attributes=True)


class DrugCategoryTree(DrugCategoryResponse):
    """Drug category with nested children"""
    children: List["DrugCategoryTree"] = []


class DrugSearchFilters(BaseSchema):
    """Filters for drug search"""
    search: Optional[str] = Field(None, description="Search term for name, generic_name, SKU, barcode")
    category_id: Optional[uuid.UUID] = None
    drug_type: Optional[str] = None
    requires_prescription: Optional[bool] = None
    is_active: Optional[bool] = True
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    manufacturer: Optional[str] = None
    supplier: Optional[str] = None


class BulkDrugUpdate(BaseSchema):
    """Schema for bulk updating multiple drugs"""
    drug_ids: List[uuid.UUID] = Field(..., min_length=1, max_length=100)
    updates: DrugUpdate