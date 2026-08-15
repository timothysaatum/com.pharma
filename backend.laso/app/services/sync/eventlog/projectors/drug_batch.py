"""
DrugBatchProjector
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

EVENT_CREATED = "drug_batch_created"
EVENT_UPDATED = "drug_batch_updated"

@ProjectorRegistry.register
class DrugBatchProjector(Projector):
    aggregate_type = AggregateType.DRUG_BATCH

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype not in (EVENT_CREATED, EVENT_UPDATED):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="unknown_event_type",
                error_message=f"DrugBatchProjector does not handle {etype!r}"
            )
        
        payload = event.payload
        drug_id = payload.get("drug_id")
        branch_id = payload.get("branch_id")
        org_id = payload.get("org_id") or payload.get("organization_id") or (str(event.org_id) if event.org_id else None)
        
        if not drug_id or not branch_id or not org_id:
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="missing_fields",
                error_message="drug_batch payload must include drug_id, branch_id, org_id"
            )

        if str(org_id) != str(event.org_id):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="org_scope_violation",
                error_message="org_id mismatch"
            )

        if payload.get("remaining_quantity", 0) < 0:
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="invalid_quantity",
                error_message="remaining_quantity must be >= 0"
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

        return ProjectorResult(status=ProjectorStatus.OK)

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        etype = event.event_type
        p = event.payload
        now = event.authored_at
        
        if etype == EVENT_CREATED:
            res = await db.execute(
                text("""
                    INSERT INTO drug_batches (
                        id, branch_id, drug_id, batch_number,
                        quantity, remaining_quantity, expiry_date,
                        cost_price, selling_price, purchase_order_id, supplier,
                        sync_version, sync_status, created_at, updated_at
                    ) VALUES (
                        CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID), :batch_number,
                        :quantity, :remaining_quantity, CAST(:expiry_date AS DATE),
                        :cost_price, :selling_price, CAST(:purchase_order_id AS UUID), :supplier,
                        1, 'synced', :now, :now
                    )
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                """),
                {
                    "id": str(event.aggregate_id),
                    "branch_id": str(p["branch_id"]),
                    "drug_id": str(p["drug_id"]),
                    "batch_number": p.get("batch_number", ""),
                    "quantity": int(p.get("quantity", 0)),
                    "remaining_quantity": int(p.get("remaining_quantity", 0)),
                    "expiry_date": _parse_date(p.get("expiry_date")),
                    "cost_price": _decimal_str(p.get("cost_price")),
                    "selling_price": _decimal_str(p.get("selling_price")),
                    "purchase_order_id": _uuid_or_none(p.get("purchase_order_id")),
                    "supplier": p.get("supplier"),
                    "now": now,
                }
            )
            
            if not res.fetchone():
                return
                
            inv_row = (await db.execute(
                text("""
                    SELECT id FROM branch_inventory
                    WHERE branch_id = CAST(:branch_id AS UUID) AND drug_id = CAST(:drug_id AS UUID)
                    ORDER BY id LIMIT 1
                """),
                {"branch_id": str(p["branch_id"]), "drug_id": str(p["drug_id"])}
            )).first()
            
            if inv_row:
                await db.execute(
                    text("""
                        UPDATE branch_inventory
                        SET quantity = quantity + :qty,
                            updated_at = :now,
                            sync_version = sync_version + 1,
                            sync_status = 'synced'
                        WHERE id = :id
                    """),
                    {"id": inv_row.id, "qty": int(p.get("remaining_quantity", 0)), "now": now}
                )
            else:
                await db.execute(
                    text("""
                        INSERT INTO branch_inventory (
                            id, branch_id, drug_id, quantity, reserved_quantity,
                            sync_version, sync_status, created_at, updated_at
                        ) VALUES (
                            CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID), :qty, 0,
                            1, 'synced', :now, :now
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "branch_id": str(p["branch_id"]),
                        "drug_id": str(p["drug_id"]),
                        "qty": int(p.get("remaining_quantity", 0)),
                        "now": now,
                    }
                )
        elif etype == EVENT_UPDATED:
            row = (await db.execute(
                text("SELECT remaining_quantity FROM drug_batches WHERE id = CAST(:id AS UUID)"),
                {"id": str(event.aggregate_id)}
            )).first()
            if not row:
                return
            
            old_qty = row.remaining_quantity
            new_qty = int(p.get("remaining_quantity", 0))
            delta = new_qty - old_qty
            
            await db.execute(
                text("""
                    UPDATE drug_batches SET
                        batch_number = :batch_number,
                        quantity = :quantity,
                        remaining_quantity = :remaining_quantity,
                        expiry_date = CAST(:expiry_date AS DATE),
                        cost_price = :cost_price,
                        selling_price = :selling_price,
                        purchase_order_id = CAST(:purchase_order_id AS UUID),
                        supplier = :supplier,
                        sync_status = 'synced',
                        sync_version = sync_version + 1,
                        updated_at = :now
                    WHERE id = CAST(:id AS UUID)
                """),
                {
                    "id": str(event.aggregate_id),
                    "batch_number": p.get("batch_number", ""),
                    "quantity": int(p.get("quantity", 0)),
                    "remaining_quantity": new_qty,
                    "expiry_date": _parse_date(p.get("expiry_date")),
                    "cost_price": _decimal_str(p.get("cost_price")),
                    "selling_price": _decimal_str(p.get("selling_price")),
                    "purchase_order_id": _uuid_or_none(p.get("purchase_order_id")),
                    "supplier": p.get("supplier"),
                    "now": now,
                }
            )
            
            if delta != 0:
                inv_row = (await db.execute(
                    text("""
                        SELECT id FROM branch_inventory
                        WHERE branch_id = CAST(:branch_id AS UUID) AND drug_id = CAST(:drug_id AS UUID)
                        ORDER BY id LIMIT 1
                    """),
                    {"branch_id": str(p["branch_id"]), "drug_id": str(p["drug_id"])}
                )).first()
                if inv_row:
                    await db.execute(
                        text("""
                            UPDATE branch_inventory SET
                                quantity = quantity + :delta,
                                updated_at = :now,
                                sync_version = sync_version + 1,
                                sync_status = 'synced'
                            WHERE id = :id
                        """),
                        {
                            "id": inv_row.id,
                            "delta": delta,
                            "now": now,
                        }
                    )


def _parse_date(v: Any):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        from datetime import date
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return v

def _uuid_or_none(v: Any):
    return str(v) if v else None

def _decimal_str(v: Any):
    if v is None:
        return None
    return str(v)
