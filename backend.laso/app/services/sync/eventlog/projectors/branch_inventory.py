"""
BranchInventoryProjector
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_envelope import AggregateType, EventEnvelope
from app.services.sync.eventlog.projector import (
    Projector,
    ProjectorRegistry,
    ProjectorResult,
    ProjectorStatus,
)

logger = logging.getLogger(__name__)

EVENT_CREATED = "branch_inventory_created"
EVENT_UPDATED = "branch_inventory_updated"

@ProjectorRegistry.register
class BranchInventoryProjector(Projector):
    aggregate_type = AggregateType.BRANCH_INVENTORY

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype not in (EVENT_CREATED, EVENT_UPDATED):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="unknown_event_type",
                error_message=f"BranchInventoryProjector does not handle {etype!r}"
            )
        
        payload = event.payload
        branch_id = payload.get("branch_id")
        drug_id = payload.get("drug_id")
        org_id = payload.get("org_id") or payload.get("organization_id") or (str(event.org_id) if event.org_id else None)

        if not branch_id or not drug_id or not org_id:
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="missing_fields",
                error_message="branch_inventory payload must include branch_id, drug_id, org_id"
            )

        if str(org_id) != str(event.org_id):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="org_scope_violation",
                error_message="org_id mismatch"
            )

        branch_exists = await db.execute(
            text("SELECT 1 FROM branches WHERE id = CAST(:branch_id AS UUID) AND organization_id = CAST(:org_id AS UUID)"),
            {"branch_id": str(branch_id), "org_id": str(org_id)}
        )
        if not branch_exists.first():
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="invalid_branch",
                error_message="Branch does not exist"
            )

        drug_exists = await db.execute(
            text("SELECT 1 FROM drugs WHERE id = CAST(:drug_id AS UUID) AND organization_id = CAST(:org_id AS UUID)"),
            {"drug_id": str(drug_id), "org_id": str(org_id)}
        )
        if not drug_exists.first():
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="invalid_drug",
                error_message="Drug does not exist"
            )

        return ProjectorResult(status=ProjectorStatus.OK)

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        etype = event.event_type
        p = event.payload
        now = event.authored_at

        if etype == EVENT_CREATED:
            await db.execute(
                text("""
                    INSERT INTO branch_inventory (
                        id, branch_id, drug_id, quantity, reserved_quantity,
                        location, selling_price, sync_version, sync_status,
                        created_at, updated_at
                    ) VALUES (
                        CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID), 0, 0,
                        :shelf_location, :branch_selling_price, 1, 'synced',
                        :now, :now
                    )
                    ON CONFLICT (branch_id, drug_id) DO NOTHING
                """),
                {
                    "id": str(event.aggregate_id),
                    "branch_id": str(p["branch_id"]),
                    "drug_id": str(p["drug_id"]),
                    "shelf_location": p.get("shelf_location"),
                    "branch_selling_price": _decimal_str(p.get("branch_selling_price")),
                    "now": now,
                }
            )
        elif etype == EVENT_UPDATED:
            # We don't touch quantity, just metadata
            # For idempotent update we update by ID
            await db.execute(
                text("""
                    UPDATE branch_inventory SET
                        location = :shelf_location,
                        selling_price = :branch_selling_price,
                        updated_at = :now,
                        sync_version = sync_version + 1,
                        sync_status = 'synced'
                    WHERE id = CAST(:id AS UUID)
                """),
                {
                    "id": str(event.aggregate_id),
                    "shelf_location": p.get("shelf_location"),
                    "branch_selling_price": _decimal_str(p.get("branch_selling_price")),
                    "now": now,
                }
            )

def _decimal_str(v: Any):
    if v is None:
        return None
    return str(v)
