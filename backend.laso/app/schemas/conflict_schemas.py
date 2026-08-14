"""
Schemas for the unresolved_conflicts read/resolve API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel


class ConflictResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_id: Optional[str]
    local_vector: Dict[str, int]
    local_snapshot: Dict[str, Any]
    incoming_vector: Dict[str, int]
    incoming_payload: Dict[str, Any]
    status: str
    resolved_at: Optional[datetime]
    resolved_by: Optional[uuid.UUID]
    created_at: datetime

    model_config = {"from_attributes": True}


class ConflictListResponse(BaseModel):
    conflicts: List[ConflictResponse]
    total: int
    pending: int


class ResolveConflictRequest(BaseModel):
    resolution: Literal["keep_local", "keep_incoming"]
