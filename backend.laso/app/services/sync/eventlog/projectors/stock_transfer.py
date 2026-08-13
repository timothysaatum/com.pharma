"""
StockTransferProjector — handles stock_transfer events (AggregateType.STOCK_TRANSFER).

The aggregate_id is the source stock_adjustment.id.

The client selects batches offline (FEFO) and sends pre-computed batch deductions
in the payload (source side only). The projector applies them without re-running
FEFO, keeping the server side idempotent and deterministic.

Event types handled:
- stock_transfer — moves quantity from source_branch to destination_branch.

Payload shape:
{
  "organization_id": "uuid",
  "source_branch_id": "uuid",
  "destination_branch_id": "uuid",
  "drug_id": "uuid",
  "transferred_by": "uuid",
  "quantity": 20,                        # total transferred (positive integer)
  "reason": "Restocking branch B",       # optional
  "destination_adjustment_id": "uuid",   # PK for the dest stock_adjustment row
  "source_previous_quantity": 80,        # branch_inventory.quantity at source before
  "source_new_quantity": 60,             # branch_inventory.quantity at source after
  "destination_previous_quantity": 20,   # branch_inventory.quantity at dest before
  "destination_new_quantity": 40,        # branch_inventory.quantity at dest after
  "batch_changes": [                     # source-side per-batch deductions (can be empty)
    {
      "batch_id": "uuid",
      "quantity_change": -20,            # signed negative (deduction from source batch)
      "batch_before": 80,
      "batch_after": 60
    }
  ]
}

Idempotency:
  - Source stock_adjustment: INSERT ON CONFLICT (id) DO NOTHING RETURNING id.
  - All side effects are guarded by checking whether the source adjustment was
    newly inserted (RETURNING id). A None result means already projected → skip.
  - Destination stock_adjustment: INSERT ON CONFLICT (id) DO NOTHING.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


@ProjectorRegistry.register
class StockTransferProjector(Projector):
    aggregate_type = AggregateType.STOCK_TRANSFER

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        if event.event_type == "stock_transfer":
            return _validate_transfer(event)
        return ProjectorResult(
            status=ProjectorStatus.REJECTED_PERMANENT,
            error_code="unknown_event_type",
            error_message=(
                f"StockTransferProjector does not handle event_type={event.event_type!r}"
            ),
        )

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        if event.event_type == "stock_transfer":
            await _apply_transfer(event, db)
        else:
            raise RuntimeError(
                f"StockTransferProjector.apply reached with unhandled "
                f"event_type={event.event_type!r}"
            )


# ── validate helpers ─────────────────────────────────────────────────────────


def _validate_transfer(event: EventEnvelope) -> ProjectorResult:
    p = event.payload

    org_id = p.get("organization_id")
    if not org_id:
        return _reject("missing_organization_id",
                       "stock_transfer payload must include organization_id")
    if str(org_id) != str(event.org_id):
        return _reject(
            "org_scope_violation",
            f"payload.organization_id ({org_id}) != envelope.org_id ({event.org_id})",
        )

    for field in ("source_branch_id", "destination_branch_id", "drug_id",
                  "transferred_by", "destination_adjustment_id"):
        if not p.get(field):
            return _reject(f"missing_{field}",
                           f"stock_transfer payload must include {field}")

    src = str(p.get("source_branch_id", ""))
    dst = str(p.get("destination_branch_id", ""))
    if src and dst and src == dst:
        return _reject("same_branch_transfer",
                       "source_branch_id and destination_branch_id must differ")

    qty = p.get("quantity")
    if qty is None or int(qty) <= 0:
        return _reject("invalid_quantity",
                       "quantity must be a positive integer")

    src_new = p.get("source_new_quantity")
    if src_new is None or int(src_new) < 0:
        return _reject("invalid_source_new_quantity",
                       "source_new_quantity must be a non-negative integer")

    dst_new = p.get("destination_new_quantity")
    if dst_new is None or int(dst_new) < 0:
        return _reject("invalid_destination_new_quantity",
                       "destination_new_quantity must be a non-negative integer")

    return ProjectorResult(status=ProjectorStatus.OK)


def _reject(code: str, message: str) -> ProjectorResult:
    return ProjectorResult(
        status=ProjectorStatus.REJECTED_PERMANENT,
        error_code=code,
        error_message=message,
    )


# ── apply helpers ─────────────────────────────────────────────────────────────


async def _apply_transfer(event: EventEnvelope, db: AsyncSession) -> None:
    p = event.payload
    now = datetime.now(timezone.utc)
    src_adj_id = str(event.aggregate_id)
    org_id = str(event.org_id)
    src_branch_id = str(p["source_branch_id"])
    dst_branch_id = str(p["destination_branch_id"])
    drug_id = str(p["drug_id"])
    transferred_by = str(p["transferred_by"])
    dst_adj_id = str(p["destination_adjustment_id"])
    quantity = int(p["quantity"])
    src_prev_qty = int(p.get("source_previous_quantity", 0))
    src_new_qty = int(p["source_new_quantity"])
    dst_prev_qty = int(p.get("destination_previous_quantity", 0))
    dst_new_qty = int(p["destination_new_quantity"])
    reason = p.get("reason")

    # ── 1. INSERT source stock_adjustment — idempotency guard ─────────────────
    result = await db.execute(
        text("""
            INSERT INTO stock_adjustments (
                id, branch_id, drug_id,
                adjustment_type, quantity_change,
                previous_quantity, new_quantity,
                reason, adjusted_by,
                transfer_to_branch_id,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID),
                CAST(:branch_id AS UUID),
                CAST(:drug_id AS UUID),
                'transfer', :qty_change,
                :prev_qty, :new_qty,
                :reason,
                CAST(:adjusted_by AS UUID),
                CAST(:transfer_to AS UUID),
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        """),
        {
            "id": src_adj_id,
            "branch_id": src_branch_id,
            "drug_id": drug_id,
            "qty_change": -quantity,
            "prev_qty": src_prev_qty,
            "new_qty": src_new_qty,
            "reason": reason,
            "adjusted_by": transferred_by,
            "transfer_to": dst_branch_id,
            "created_at": event.authored_at,
            "updated_at": event.authored_at,
        },
    )
    if result.fetchone() is None:
        return  # already projected — skip all side effects

    # ── 2. INSERT destination stock_adjustment ────────────────────────────────
    await db.execute(
        text("""
            INSERT INTO stock_adjustments (
                id, branch_id, drug_id,
                adjustment_type, quantity_change,
                previous_quantity, new_quantity,
                reason, adjusted_by,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID),
                CAST(:branch_id AS UUID),
                CAST(:drug_id AS UUID),
                'transfer', :qty_change,
                :prev_qty, :new_qty,
                :reason,
                CAST(:adjusted_by AS UUID),
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
        """),
        {
            "id": dst_adj_id,
            "branch_id": dst_branch_id,
            "drug_id": drug_id,
            "qty_change": quantity,
            "prev_qty": dst_prev_qty,
            "new_qty": dst_new_qty,
            "reason": reason,
            "adjusted_by": transferred_by,
            "created_at": event.authored_at,
            "updated_at": event.authored_at,
        },
    )

    # ── 3. UPDATE source branch_inventory ─────────────────────────────────────
    await db.execute(
        text("""
            UPDATE branch_inventory
               SET quantity = :new_qty,
                   updated_at = :now,
                   sync_version = sync_version + 1,
                   sync_status = 'synced'
             WHERE id = (
                       SELECT id FROM branch_inventory
                        WHERE branch_id = :branch_id
                          AND drug_id   = :drug_id
                     ORDER BY id LIMIT 1
                   )
        """),
        {"new_qty": src_new_qty, "branch_id": src_branch_id,
         "drug_id": drug_id, "now": now},
    )

    # ── 4. UPDATE destination branch_inventory ────────────────────────────────
    await db.execute(
        text("""
            UPDATE branch_inventory
               SET quantity = :new_qty,
                   updated_at = :now,
                   sync_version = sync_version + 1,
                   sync_status = 'synced'
             WHERE id = (
                       SELECT id FROM branch_inventory
                        WHERE branch_id = :branch_id
                          AND drug_id   = :drug_id
                     ORDER BY id LIMIT 1
                   )
        """),
        {"new_qty": dst_new_qty, "branch_id": dst_branch_id,
         "drug_id": drug_id, "now": now},
    )

    # ── 5. Per-batch changes (source side) ────────────────────────────────────
    batch_changes: List[Dict[str, Any]] = p.get("batch_changes") or []

    for bc in batch_changes:
        batch_id = _str_or_none(bc.get("batch_id"))
        bc_qty = int(bc.get("quantity_change", 0))
        batch_before = int(bc.get("batch_before", 0))
        batch_after = int(bc.get("batch_after", 0))

        if batch_id:
            await db.execute(
                text("""
                    UPDATE drug_batches
                       SET remaining_quantity = :batch_after,
                           updated_at = :now,
                           sync_version = sync_version + 1,
                           sync_status = 'synced'
                     WHERE id = :batch_id
                """),
                {"batch_after": batch_after, "now": now, "batch_id": batch_id},
            )

        # transfer_out movement for source
        await db.execute(
            text("""
                INSERT INTO inventory_movements (
                    id, organization_id, branch_id, drug_id, batch_id,
                    movement_type, quantity_change,
                    quantity_before, quantity_after,
                    batch_quantity_before, batch_quantity_after,
                    source_type, source_id,
                    reason, created_by, occurred_at
                ) VALUES (
                    CAST(:id AS UUID),
                    CAST(:org_id AS UUID),
                    CAST(:branch_id AS UUID),
                    CAST(:drug_id AS UUID),
                    CAST(:batch_id AS UUID),
                    'transfer_out', :qty_change,
                    :qty_before, :qty_after,
                    :batch_before, :batch_after,
                    'stock_adjustment', CAST(:source_id AS UUID),
                    :reason,
                    CAST(:created_by AS UUID),
                    :occurred_at
                )
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "branch_id": src_branch_id,
                "drug_id": drug_id,
                "batch_id": batch_id,
                "qty_change": bc_qty,
                "qty_before": src_prev_qty,
                "qty_after": src_new_qty,
                "batch_before": batch_before,
                "batch_after": batch_after,
                "source_id": src_adj_id,
                "reason": reason,
                "created_by": transferred_by,
                "occurred_at": event.authored_at,
            },
        )

    # transfer_in movement for destination (always at aggregate level)
    await db.execute(
        text("""
            INSERT INTO inventory_movements (
                id, organization_id, branch_id, drug_id,
                movement_type, quantity_change,
                quantity_before, quantity_after,
                source_type, source_id,
                reason, created_by, occurred_at
            ) VALUES (
                CAST(:id AS UUID),
                CAST(:org_id AS UUID),
                CAST(:branch_id AS UUID),
                CAST(:drug_id AS UUID),
                'transfer_in', :qty_change,
                :qty_before, :qty_after,
                'stock_adjustment', CAST(:source_id AS UUID),
                :reason,
                CAST(:created_by AS UUID),
                :occurred_at
            )
        """),
        {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "branch_id": dst_branch_id,
            "drug_id": drug_id,
            "qty_change": quantity,
            "qty_before": dst_prev_qty,
            "qty_after": dst_new_qty,
            "source_id": dst_adj_id,
            "reason": reason,
            "created_by": transferred_by,
            "occurred_at": event.authored_at,
        },
    )

    # If no batch_changes, write one aggregate-level transfer_out for the audit trail
    if not batch_changes:
        await db.execute(
            text("""
                INSERT INTO inventory_movements (
                    id, organization_id, branch_id, drug_id,
                    movement_type, quantity_change,
                    quantity_before, quantity_after,
                    source_type, source_id,
                    reason, created_by, occurred_at
                ) VALUES (
                    CAST(:id AS UUID),
                    CAST(:org_id AS UUID),
                    CAST(:branch_id AS UUID),
                    CAST(:drug_id AS UUID),
                    'transfer_out', :qty_change,
                    :qty_before, :qty_after,
                    'stock_adjustment', CAST(:source_id AS UUID),
                    :reason,
                    CAST(:created_by AS UUID),
                    :occurred_at
                )
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "branch_id": src_branch_id,
                "drug_id": drug_id,
                "qty_change": -quantity,
                "qty_before": src_prev_qty,
                "qty_after": src_new_qty,
                "source_id": src_adj_id,
                "reason": reason,
                "created_by": transferred_by,
                "occurred_at": event.authored_at,
            },
        )


# ── small conversions ────────────────────────────────────────────────────────


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)
