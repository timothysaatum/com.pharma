"""
PurchaseOrderProjector
"""
from __future__ import annotations

import json
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

EVENT_CREATED = "purchase_order_created"
EVENT_UPDATED = "purchase_order_updated"
VALID_STATUSES = {"draft", "pending", "approved", "ordered", "received", "cancelled"}

@ProjectorRegistry.register
class PurchaseOrderProjector(Projector):
    aggregate_type = AggregateType.PURCHASE_ORDER

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype not in (EVENT_CREATED, EVENT_UPDATED):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="unknown_event_type",
                error_message=f"PurchaseOrderProjector does not handle {etype!r}"
            )
        
        payload = event.payload
        branch_id = payload.get("branch_id")
        org_id = payload.get("org_id") or payload.get("organization_id") or (str(event.org_id) if event.org_id else None)
        status = payload.get("status")

        if not branch_id or not org_id:
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="missing_fields",
                error_message="purchase_order payload must include branch_id, org_id"
            )

        if str(org_id) != str(event.org_id):
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="org_scope_violation",
                error_message="org_id mismatch"
            )

        if status not in VALID_STATUSES:
            return ProjectorResult(
                status=ProjectorStatus.REJECTED_PERMANENT,
                error_code="invalid_status",
                error_message=f"Invalid status: {status}"
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
        po_id = str(event.aggregate_id)

        if etype == EVENT_CREATED:
            supplier_id = _uuid_or_none(p.get("supplier_id"))
            if not supplier_id:
                supp_name = p.get("supplier_name") or "Default Supplier"
                supp_res = await db.execute(
                    text("""
                        SELECT id FROM suppliers 
                        WHERE organization_id = CAST(:org_id AS UUID) 
                        ORDER BY created_at ASC LIMIT 1
                    """),
                    {"org_id": str(p["org_id"])},
                )
                found_supp = supp_res.first()
                if found_supp:
                    supplier_id = str(found_supp[0])
                else:
                    new_supp_id = str(uuid.uuid4())
                    await db.execute(
                        text("""
                            INSERT INTO suppliers (
                                id, organization_id, name, is_active, is_deleted,
                                total_orders, total_value,
                                sync_version, sync_status,
                                created_at, updated_at
                            ) VALUES (
                                CAST(:id AS UUID), CAST(:org_id AS UUID), :name, true, false,
                                0, 0,
                                1, 'synced',
                                :now, :now
                            )
                            ON CONFLICT (id) DO NOTHING
                        """),
                        {
                            "id": new_supp_id,
                            "org_id": str(p["org_id"]),
                            "name": supp_name,
                            "now": now,
                        },
                    )
                    supplier_id = new_supp_id

            res = await db.execute(
                text("""
                    INSERT INTO purchase_orders (
                        id, organization_id, branch_id, po_number,
                        supplier_id, subtotal, tax_amount, shipping_cost, total_amount,
                        status, ordered_by, expected_delivery_date, received_date, notes,
                        sync_version, sync_status, created_at, updated_at
                    ) VALUES (
                        CAST(:id AS UUID), CAST(:org_id AS UUID), CAST(:branch_id AS UUID), :po_number,
                        CAST(:supplier_id AS UUID), :subtotal, :tax_amount, :shipping_cost, :total_amount,
                        :status, CAST(:ordered_by AS UUID), CAST(:expected_delivery_date AS DATE),
                        CAST(:received_date AS DATE), :notes,
                        1, 'synced', :now, :now
                    )
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                """),
                {
                    "id": po_id,
                    "org_id": str(p["org_id"]),
                    "branch_id": str(p["branch_id"]),
                    "po_number": p.get("po_number", "PO-" + str(uuid.uuid4())[:8]),
                    "supplier_id": supplier_id,
                    "subtotal": _decimal_str(p.get("subtotal", 0)),
                    "tax_amount": _decimal_str(p.get("tax_amount", 0)),
                    "shipping_cost": _decimal_str(p.get("shipping_cost", 0)),
                    "total_amount": _decimal_str(p.get("total_amount", 0)),
                    "status": p.get("status", "draft"),
                    "ordered_by": _uuid_or_none(p.get("ordered_by")) or str(event.authored_by),
                    "expected_delivery_date": _parse_date(p.get("expected_delivery_date")),
                    "received_date": _parse_date(p.get("received_date")),
                    "notes": p.get("notes"),
                    "now": now,
                }
            )
            
            if not res.fetchone():
                return
                
            items = p.get("items", [])
            for item in items:
                qty_ord = int(item.get("quantity_ordered") or item.get("quantity") or 0)
                qty_rec = int(item.get("quantity_received", 0))
                u_cost = float(item.get("unit_cost", 0))
                t_cost = float(item["total_cost"]) if "total_cost" in item else (u_cost * qty_ord)
                await db.execute(
                    text("""
                        INSERT INTO purchase_order_items (
                            id, purchase_order_id, drug_id,
                            quantity_ordered, quantity_received,
                            unit_cost, total_cost,
                            created_at, updated_at
                        ) VALUES (
                            CAST(:id AS UUID), CAST(:po_id AS UUID), CAST(:drug_id AS UUID),
                            :qty_ordered, :qty_received,
                            :unit_cost, :total_cost,
                            :now, :now
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "po_id": po_id,
                        "drug_id": str(item["drug_id"]),
                        "qty_ordered": qty_ord,
                        "qty_received": qty_rec,
                        "unit_cost": _decimal_str(u_cost),
                        "total_cost": _decimal_str(t_cost),
                        "now": now,
                    }
                )
                
        elif etype == EVENT_UPDATED:
            await db.execute(
                text("""
                    UPDATE purchase_orders SET
                        status = :status,
                        expected_delivery_date = CAST(:expected_delivery_date AS DATE),
                        received_date = CAST(:received_date AS DATE),
                        notes = :notes,
                        updated_at = :now,
                        sync_version = sync_version + 1,
                        sync_status = 'synced'
                    WHERE id = CAST(:id AS UUID)
                """),
                {
                    "id": po_id,
                    "status": p.get("status"),
                    "expected_delivery_date": _parse_date(p.get("expected_delivery_date")),
                    "received_date": _parse_date(p.get("received_date")),
                    "notes": p.get("notes"),
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
