"""
Unit tests for new projectors:
- DrugBatchProjector
- BranchInventoryProjector
- PurchaseOrderProjector

Validates registry mappings, payload schema validation, error rejection, and envelope building.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.event_envelope import (
    EventEnvelope,
    AggregateType,
    EventStatus,
    GENESIS_HASH,
    compute_hash_self,
)
from app.services.sync.eventlog.projector import (
    ProjectorRegistry,
    ProjectorResult,
    ProjectorStatus,
)
import app.services.sync.eventlog.projectors  # registers all projectors
from app.services.sync.eventlog.projectors.drug_batch import DrugBatchProjector
from app.services.sync.eventlog.projectors.branch_inventory import BranchInventoryProjector
from app.services.sync.eventlog.projectors.purchase_order import PurchaseOrderProjector


@pytest.mark.asyncio
async def test_projector_registry_mappings():
    """Verify all new aggregate types are registered in ProjectorRegistry."""
    expected_types = [
        AggregateType.DRUG_BATCH,
        AggregateType.BRANCH_INVENTORY,
        AggregateType.PURCHASE_ORDER,
    ]
    for agg_type in expected_types:
        projector = ProjectorRegistry.get(agg_type)
        assert projector is not None, f"Missing projector registration for {agg_type}"


@pytest.mark.asyncio
async def test_drug_batch_projector_validation():
    projector = DrugBatchProjector()
    session = AsyncMock()

    # Missing drug_id
    envelope = EventEnvelope(
        event_id="01J1234567890ABCDEFGH00001",
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.DRUG_BATCH,
        event_type="drug_batch_created",
        schema_version=1,
        payload={
            "branch_id": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "remaining_quantity": 10,
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        hash_prev=GENESIS_HASH,
        hash_self="0" * 64,
    )
    result = await projector.validate(envelope, session)
    assert result.status == ProjectorStatus.REJECTED_PERMANENT
    assert "drug_id" in (result.error_message or "").lower()

    # Negative quantity
    envelope_neg = EventEnvelope(
        event_id="01J1234567890ABCDEFGH00002",
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.DRUG_BATCH,
        event_type="drug_batch_created",
        schema_version=1,
        payload={
            "drug_id": str(uuid.uuid4()),
            "branch_id": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "remaining_quantity": -5,
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        hash_prev=GENESIS_HASH,
        hash_self="0" * 64,
    )
    result_neg = await projector.validate(envelope_neg, session)
    assert result_neg.status == ProjectorStatus.REJECTED_PERMANENT


@pytest.mark.asyncio
async def test_branch_inventory_projector_validation():
    projector = BranchInventoryProjector()
    session = AsyncMock()

    # Missing branch_id / drug_id
    envelope = EventEnvelope(
        event_id="01J1234567890ABCDEFGH00003",
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.BRANCH_INVENTORY,
        event_type="branch_inventory_created",
        schema_version=1,
        payload={
            "organization_id": str(uuid.uuid4()),
            "shelf_location": "A-1",
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        hash_prev=GENESIS_HASH,
        hash_self="0" * 64,
    )
    result = await projector.validate(envelope, session)
    assert result.status == ProjectorStatus.REJECTED_PERMANENT


@pytest.mark.asyncio
async def test_purchase_order_projector_validation():
    projector = PurchaseOrderProjector()
    session = AsyncMock()

    # Invalid status
    org_id = uuid.uuid4()
    envelope = EventEnvelope(
        event_id="01J1234567890ABCDEFGH00004",
        aggregate_id=uuid.uuid4(),
        aggregate_type=AggregateType.PURCHASE_ORDER,
        event_type="purchase_order_created",
        schema_version=1,
        payload={
            "branch_id": str(uuid.uuid4()),
            "organization_id": str(org_id),
            "status": "invalid_status_xyz",
            "items": [],
        },
        dependencies=[],
        authored_at=datetime.now(timezone.utc),
        authored_by=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        org_id=org_id,
        hash_prev=GENESIS_HASH,
        hash_self="0" * 64,
    )
    result = await projector.validate(envelope, session)
    assert result.status == ProjectorStatus.REJECTED_PERMANENT
    assert "status" in (result.error_message or "").lower()
