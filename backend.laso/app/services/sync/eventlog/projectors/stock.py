"""
StockProjector — handles stock_adjusted events (AggregateType.STOCK).

The aggregate_id is the stock_adjustment.id (unique per adjustment event).

The client selects batches offline (FEFO) and sends pre-computed batch changes
in the payload. The projector applies them without re-running FEFO selection,
keeping the server side idempotent and deterministic.

Event types handled:
- stock_adjusted — covers damage, theft, expired, correction, return, purchase_receipt.
  Transfer (aggregate_type=stock_transfer) is a separate projector (Phase 1d).

Payload shape for stock_adjusted:
{
  "organization_id": "uuid",
  "branch_id": "uuid",
  "drug_id": "uuid",
  "adjusted_by": "uuid",
  "adjustment_type": "damage",        # one of the 7 valid types
  "quantity_change": -5,              # signed (negative = deduction, positive = addition)
  "previous_quantity": 100,           # branch_inventory.quantity before
  "new_quantity": 95,                 # branch_inventory.quantity after
  "reason": "Water damage",           # optional
  "batch_changes": [                  # list of per-batch deltas (can be empty for correction)
    {
      "batch_id": "uuid",             # required unless is_new_batch=true
      "quantity_change": -5,          # signed
      "batch_before": 30,
      "batch_after": 25,
      "is_new_batch": false
    }
  ],
  // For purchase_receipt / return with new batch only:
  "new_batch": {
    "batch_id": "uuid",
    "batch_number": "LOT-2026-001",
    "expiry_date": "2028-12-31",
    "quantity": 50,
    "cost_price": "30.00",            # optional
    "selling_price": "50.00",         # optional
    "purchase_order_id": "uuid"       # optional
  }
}

Idempotency:
  - stock_adjustments INSERT ON CONFLICT (id) DO NOTHING.
  - All side effects (batch + inventory updates, movements) are guarded by checking
    if the adjustment was newly inserted (RETURNING id).
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

_VALID_ADJUSTMENT_TYPES = {
    "damage", "expired", "theft", "return",
    "correction", "transfer", "purchase_receipt",
}
_MOVEMENT_TYPE_MAP: Dict[str, str] = {
    "damage": "damage",
    "expired": "expired",
    "theft": "theft",
    "return": "return",
    "correction": "correction",
    "transfer": "transfer_out",
    "purchase_receipt": "purchase_receipt",
}


@ProjectorRegistry.register
class StockProjector(Projector):
    aggregate_type = AggregateType.STOCK

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype == "stock_adjusted":
            return _validate_adjusted(event)
        return ProjectorResult(
            status=ProjectorStatus.REJECTED_PERMANENT,
            error_code="unknown_event_type",
            error_message=(
                f"StockProjector does not handle event_type={etype!r}"
            ),
        )

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        if event.event_type == "stock_adjusted":
            await _apply_adjusted(event, db)
        else:
            raise RuntimeError(
                f"StockProjector.apply reached with unhandled "
                f"event_type={event.event_type!r}"
            )


# ── validate helpers ─────────────────────────────────────────────────────────


def _validate_adjusted(event: EventEnvelope) -> ProjectorResult:
    p = event.payload

    org_id = p.get("organization_id")
    if not org_id:
        return _reject("missing_organization_id",
                       "stock_adjusted payload must include organization_id")
    if str(org_id) != str(event.org_id):
        return _reject(
            "org_scope_violation",
            f"payload.organization_id ({org_id}) != envelope.org_id ({event.org_id})",
        )

    for field in ("branch_id", "drug_id", "adjusted_by"):
        if not p.get(field):
            return _reject(f"missing_{field}",
                           f"stock_adjusted payload must include {field}")

    adj_type = p.get("adjustment_type", "")
    if adj_type not in _VALID_ADJUSTMENT_TYPES:
        return _reject(
            "invalid_adjustment_type",
            f"adjustment_type={adj_type!r} not in {sorted(_VALID_ADJUSTMENT_TYPES)}",
        )

    qty_change = p.get("quantity_change")
    if qty_change is None or qty_change == 0:
        return _reject("invalid_quantity_change",
                       "quantity_change must be a non-zero integer")

    new_qty = p.get("new_quantity")
    if new_qty is None or int(new_qty) < 0:
        return _reject("invalid_new_quantity",
                       "new_quantity must be a non-negative integer")

    return ProjectorResult(status=ProjectorStatus.OK)


def _reject(code: str, message: str) -> ProjectorResult:
    return ProjectorResult(
        status=ProjectorStatus.REJECTED_PERMANENT,
        error_code=code,
        error_message=message,
    )


# ── apply helpers ─────────────────────────────────────────────────────────────


async def _apply_adjusted(event: EventEnvelope, db: AsyncSession) -> None:
    p = event.payload
    now = datetime.now(timezone.utc)
    adj_id = str(event.aggregate_id)
    org_id = str(event.org_id)
    branch_id = str(p["branch_id"])
    drug_id = str(p["drug_id"])
    adjusted_by = str(p["adjusted_by"])
    adj_type = p["adjustment_type"]
    qty_change = int(p["quantity_change"])
    prev_qty = int(p["previous_quantity"])
    new_qty = int(p["new_quantity"])
    reason = p.get("reason")

    # ── 1. INSERT new batch if payload provides one (purchase_receipt / return) ──
    new_batch = p.get("new_batch")
    if new_batch:
        await _insert_new_batch(db, branch_id, drug_id, new_batch, now)

    # ── 2. INSERT stock_adjustment — idempotent guard ─────────────────────────
    result = await db.execute(
        text("""
            INSERT INTO stock_adjustments (
                id, branch_id, drug_id,
                adjustment_type, quantity_change,
                previous_quantity, new_quantity,
                reason, adjusted_by,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID),
                :adj_type, :qty_change,
                :prev_qty, :new_qty,
                :reason,
                CAST(:adjusted_by AS UUID),
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        """),
        {
            "id": adj_id,
            "branch_id": branch_id,
            "drug_id": drug_id,
            "adj_type": adj_type,
            "qty_change": qty_change,
            "prev_qty": prev_qty,
            "new_qty": new_qty,
            "reason": reason,
            "adjusted_by": adjusted_by,
            "created_at": event.authored_at,
            "updated_at": event.authored_at,
        },
    )
    if result.fetchone() is None:
        return  # already projected — skip all side effects

    # ── 3. UPDATE branch_inventory ────────────────────────────────────────────
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
        {
            "new_qty": new_qty,
            "branch_id": branch_id,
            "drug_id": drug_id,
            "now": now,
        },
    )

    # ── 4. Per-batch changes ──────────────────────────────────────────────────
    batch_changes: List[Dict[str, Any]] = p.get("batch_changes") or []
    movement_type = _MOVEMENT_TYPE_MAP.get(adj_type, "correction")

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
                    :movement_type, :qty_change,
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
                "branch_id": branch_id,
                "drug_id": drug_id,
                "batch_id": batch_id,
                "movement_type": movement_type,
                "qty_change": bc_qty,
                "qty_before": prev_qty,
                "qty_after": new_qty,
                "batch_before": batch_before,
                "batch_after": batch_after,
                "source_id": adj_id,
                "reason": reason,
                "created_by": adjusted_by,
                "occurred_at": event.authored_at,
            },
        )

    # When no batch_changes provided (simple correction), write one movement
    # at the aggregate level for the audit trail.
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
                    :movement_type, :qty_change,
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
                "branch_id": branch_id,
                "drug_id": drug_id,
                "movement_type": movement_type,
                "qty_change": qty_change,
                "qty_before": prev_qty,
                "qty_after": new_qty,
                "source_id": adj_id,
                "reason": reason,
                "created_by": adjusted_by,
                "occurred_at": event.authored_at,
            },
        )


async def _insert_new_batch(
    db: AsyncSession,
    branch_id: str,
    drug_id: str,
    nb: Dict[str, Any],
    now: datetime,
) -> None:
    await db.execute(
        text("""
            INSERT INTO drug_batches (
                id, branch_id, drug_id,
                batch_number, quantity, remaining_quantity,
                expiry_date,
                cost_price, selling_price,
                purchase_order_id,
                version_id, sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:branch_id AS UUID), CAST(:drug_id AS UUID),
                :batch_number, :quantity, :remaining_quantity,
                :expiry_date,
                :cost_price, :selling_price,
                CAST(:purchase_order_id AS UUID),
                1, 1, 'synced',
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
        """),
        {
            "id": str(nb["batch_id"]),
            "branch_id": branch_id,
            "drug_id": drug_id,
            "batch_number": nb["batch_number"],
            "quantity": int(nb["quantity"]),
            "remaining_quantity": int(nb.get("remaining_quantity", nb["quantity"])),
            "expiry_date": _parse_date(nb["expiry_date"]),
            "cost_price": _decimal_str_or_none(nb.get("cost_price")),
            "selling_price": _decimal_str_or_none(nb.get("selling_price")),
            "purchase_order_id": _str_or_none(nb.get("purchase_order_id")),
            "created_at": now,
            "updated_at": now,
        },
    )


# ── small conversions ────────────────────────────────────────────────────────


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _decimal_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _parse_date(v: Any) -> Optional[Any]:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        from datetime import date
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return v
