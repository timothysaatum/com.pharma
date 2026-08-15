from pydantic import BaseModel, Field
import uuid
from datetime import datetime
from typing import List

class LeaseItemRequest(BaseModel):
    drug_id: uuid.UUID
    requested_quantity: int = Field(..., ge=1)

class LeaseAcquireRequest(BaseModel):
    branch_id: uuid.UUID
    terminal_id: str = Field(..., min_length=1, max_length=100)
    items: List[LeaseItemRequest]
    ttl_seconds: int = Field(3600, ge=60, le=86400)

class LeaseResponse(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    drug_id: uuid.UUID
    terminal_id: str
    leased_quantity: int
    consumed_quantity: int
    expires_at: datetime
    status: str

class LeaseAcquireResponse(BaseModel):
    leases: List[LeaseResponse]
