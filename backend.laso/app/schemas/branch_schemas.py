"""
Branch Schemas
Complete schemas for branch/location management
"""
from pydantic import Field, field_validator, EmailStr, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid
import re

from app.schemas.base_schemas import (
    BaseSchema, TimestampSchema, SyncSchema
)


class BranchAddress(BaseSchema):
    """Structured address schema"""
    street: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    zip_code: Optional[str] = Field(None, max_length=20)
    country: str = Field(default="Ghana", max_length=100)
    
    @field_validator('zip_code')
    @classmethod
    def validate_zip(cls, v: Optional[str]) -> Optional[str]:
        """Validate zip code format"""
        if v and not re.match(r'^[A-Z0-9\s\-]+$', v, re.IGNORECASE):
            raise ValueError('Invalid zip code format')
        return v


class OperatingHours(BaseSchema):
    """Operating hours for a single day"""
    open_time: Optional[str] = Field(None, pattern=r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$')
    close_time: Optional[str] = Field(None, pattern=r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$')
    is_closed: bool = Field(default=False, description="Is branch closed on this day")
    
    @field_validator('close_time')
    @classmethod
    def validate_times(cls, v: Optional[str], info) -> Optional[str]:
        """Ensure close time is after open time"""
        if v and 'open_time' in info.data and info.data['open_time']:
            if not info.data.get('is_closed', False):
                open_time = info.data['open_time']
                if v <= open_time:
                    raise ValueError('Close time must be after open time')
        return v


class WeeklyOperatingHours(BaseSchema):
    """Weekly operating hours"""
    monday: Optional[OperatingHours] = None
    tuesday: Optional[OperatingHours] = None
    wednesday: Optional[OperatingHours] = None
    thursday: Optional[OperatingHours] = None
    friday: Optional[OperatingHours] = None
    saturday: Optional[OperatingHours] = None
    sunday: Optional[OperatingHours] = None


class BranchBase(BaseSchema):
    """Base branch fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Branch name")
    code: Optional[str] = Field(
        None, 
        min_length=2, 
        max_length=50,
        description="Unique branch code (e.g., BR-001, MAIN, DOWNTOWN)"
    )
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[EmailStr] = None
    address: Optional[BranchAddress] = None
    manager_id: Optional[uuid.UUID] = Field(None, description="User ID of branch manager")
    operating_hours: Optional[WeeklyOperatingHours] = None
    is_active: bool = Field(default=True)
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v

        if not re.match(r'^[A-Z0-9\-_]+$', v, re.IGNORECASE):
            raise ValueError(
                'Branch code must contain only letters, numbers, hyphens, and underscores'
            )
        return v.upper()
    
    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        """Validate phone number format"""
        if v:
            # Remove spaces and special characters for validation
            cleaned = re.sub(r'[\s\-\(\)]', '', v)
            if not re.match(r'^\+?[0-9]{10,15}$', cleaned):
                raise ValueError('Invalid phone number format')
        return v


class BranchCreate(BranchBase):
    """Schema for creating a branch"""
    organization_id: Optional[uuid.UUID] = None


class BranchUpdate(BaseSchema):
    """Schema for updating a branch (all fields optional)"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code: Optional[str] = Field(None, min_length=2, max_length=50)
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[BranchAddress] = None
    manager_id: Optional[uuid.UUID] = None
    operating_hours: Optional[WeeklyOperatingHours] = None
    is_active: Optional[bool] = None
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v

        if not re.match(r'^[A-Z0-9\-_]+$', v, re.IGNORECASE):
            raise ValueError(
                'Branch code must contain only letters, numbers, hyphens, and underscores'
            )
        return v.upper()


class BranchResponse(BranchBase, TimestampSchema, SyncSchema):
    """Schema for branch API responses"""
    id: uuid.UUID
    organization_id: uuid.UUID
    
    # Manager details (if populated)
    manager_name: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)


class BranchWithStats(BranchResponse):
    """Branch response with statistics"""
    total_inventory_items: int = 0
    total_inventory_value: float = 0.0
    low_stock_count: int = 0
    total_sales_today: float = 0.0
    total_sales_month: float = 0.0
    active_users_count: int = 0


class BranchListItem(BaseSchema):
    """Simplified branch info for lists"""
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: str
    is_active: bool
    manager_id: Optional[uuid.UUID] = None
    manager_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class BranchAssignment(BaseSchema):
    """Schema for assigning users to branches"""
    user_id: uuid.UUID
    branch_ids: List[uuid.UUID] = Field(..., min_length=1, description="List of branch IDs to assign")
    
    @field_validator("branch_ids", mode="after")
    @classmethod
    def serialize_uuid_list(cls, values):
        return [str(v) for v in values]



class BranchTransferRequest(BaseSchema):
    """Schema for requesting stock transfer between branches"""
    from_branch_id: uuid.UUID
    to_branch_id: uuid.UUID
    items: List[Dict[str, Any]] = Field(
        ...,
        description="List of {drug_id, quantity} to transfer"
    )
    reason: str = Field(..., min_length=1, max_length=500)
    requested_by: uuid.UUID


class BranchSearchFilters(BaseSchema):
    """Filters for branch search"""
    search: Optional[str] = Field(None, description="Search in name, code, city")
    is_active: Optional[bool] = Field(None, description="Filter by active status")
    manager_id: Optional[uuid.UUID] = Field(None, description="Filter by manager")
    state: Optional[str] = Field(None, description="Filter by state/region")
    city: Optional[str] = Field(None, description="Filter by city")