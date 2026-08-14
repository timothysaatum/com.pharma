"""
SaleProjector — handles sale_created / sale_voided events.

sale_created:
  - INSERT into sales + sale_items + sale_item_batch_allocations.
  - Stock deduction: UPDATE drug_batches (per allocation) and
    UPDATE branch_inventory (per drug/item total).
  - Audit trail: INSERT inventory_movements + stock_adjustments.
  Idempotent: INSERT ON CONFLICT (id) DO NOTHING on sales; stock
  deduction only fires when the sale INSERT produced a new row.

sale_voided:
  - Restore stock from the existing batch_allocations in the DB.
  - UPDATE sales.status to 'cancelled'.
  - Audit trail: INSERT inventory_movements (refund) + stock_adjustments (return).
  Idempotent: the WHERE status != 'cancelled' guard makes repeated
  voids no-ops.

Dependencies:
  - sale_created has no strict aggregate deps for walk-in sales. When
    customer_id is present the client MUST declare the matching
    customer_created event as a dependency so the FK is satisfied.
  - sale_voided MUST declare its sale_created event as a dependency.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

_VALID_PAYMENT_METHODS = {
    "cash", "card", "mobile_money", "insurance", "credit", "split",
}
_VALID_PAYMENT_STATUSES = {
    "pending", "completed", "partial", "refunded", "cancelled",
}
_VALID_SALE_STATUSES = {
    "draft", "completed", "cancelled", "refunded", "partially_refunded",
}


@ProjectorRegistry.register
class SaleProjector(Projector):
    aggregate_type = AggregateType.SALE

    async def validate(
        self, event: EventEnvelope, db: AsyncSession
    ) -> ProjectorResult:
        etype = event.event_type
        if etype == "sale_created":
            return _validate_created(event)
        if etype == "sale_voided":
            return await _validate_voided(event, db)
        return ProjectorResult(
            status=ProjectorStatus.REJECTED_PERMANENT,
            error_code="unknown_event_type",
            error_message=(
                f"SaleProjector does not handle event_type={etype!r}"
            ),
        )

    async def apply(self, event: EventEnvelope, db: AsyncSession) -> None:
        etype = event.event_type
        if etype == "sale_created":
            await _apply_created(event, db)
            return
        if etype == "sale_voided":
            await _apply_voided(event, db)
            return
        raise RuntimeError(
            f"SaleProjector.apply reached with unhandled event_type={etype!r}"
        )


# ── validate helpers ─────────────────────────────────────────────────────────


def _validate_created(event: EventEnvelope) -> ProjectorResult:
    payload = event.payload

    org_id = payload.get("organization_id")
    if not org_id:
        return _reject(
            "missing_organization_id",
            "sale_created payload must include organization_id",
        )
    if str(org_id) != str(event.org_id):
        return _reject(
            "org_scope_violation",
            f"payload.organization_id ({org_id}) != envelope.org_id ({event.org_id})",
        )

    if not payload.get("sale_number"):
        return _reject("missing_sale_number", "sale_created must include sale_number")

    if not payload.get("cashier_id"):
        return _reject("missing_cashier_id", "sale_created must include cashier_id")

    pm = payload.get("payment_method", "")
    if pm not in _VALID_PAYMENT_METHODS:
        return _reject(
            "invalid_payment_method",
            f"payment_method={pm!r} not in {sorted(_VALID_PAYMENT_METHODS)}",
        )

    ps = payload.get("payment_status", "completed")
    if ps not in _VALID_PAYMENT_STATUSES:
        return _reject(
            "invalid_payment_status",
            f"payment_status={ps!r} not in {sorted(_VALID_PAYMENT_STATUSES)}",
        )

    status = payload.get("status", "completed")
    if status not in _VALID_SALE_STATUSES:
        return _reject(
            "invalid_status",
            f"status={status!r} not in {sorted(_VALID_SALE_STATUSES)}",
        )

    items = payload.get("items") or []
    if not items:
        return _reject("missing_items", "sale_created must include at least one item")

    for i, item in enumerate(items):
        if not item.get("item_id"):
            return _reject("missing_item_id", f"items[{i}].item_id is required")
        if not item.get("drug_id"):
            return _reject("missing_drug_id", f"items[{i}].drug_id is required")
        if not item.get("drug_name"):
            return _reject("missing_drug_name", f"items[{i}].drug_name is required")
        qty = item.get("quantity", 0)
        if not isinstance(qty, int) or qty <= 0:
            return _reject(
                "invalid_item_quantity",
                f"items[{i}].quantity must be a positive integer, got {qty!r}",
            )

    return ProjectorResult(status=ProjectorStatus.OK)


async def _validate_voided(
    event: EventEnvelope, db: AsyncSession
) -> ProjectorResult:
    row = (
        await db.execute(
            text(
                "SELECT id FROM sales"
                " WHERE id = :sale_id"
                "   AND organization_id = :org_id"
            ),
            {"sale_id": str(event.aggregate_id), "org_id": str(event.org_id)},
        )
    ).first()
    if row is None:
        return _reject(
            "sale_not_found",
            f"Sale {event.aggregate_id} not found for org {event.org_id}. "
            "Client must declare sale_created as a dependency.",
        )
    return ProjectorResult(status=ProjectorStatus.OK)


def _reject(code: str, message: str) -> ProjectorResult:
    return ProjectorResult(
        status=ProjectorStatus.REJECTED_PERMANENT,
        error_code=code,
        error_message=message,
    )


# ── apply helpers ────────────────────────────────────────────────────────────


async def _apply_created(event: EventEnvelope, db: AsyncSession) -> None:
    payload = event.payload
    now = datetime.now(timezone.utc)
    sale_id = str(event.aggregate_id)
    org_id = str(event.org_id)
    branch_id = str(payload["branch_id"])

    # ── 1. INSERT sale header (idempotent) ───────────────────────────────────
    result = await db.execute(
        text("""
            INSERT INTO sales (
                id, organization_id, branch_id, sale_number,
                customer_id, customer_name,
                subtotal, discount_amount, tax_amount, total_amount,
                price_contract_id, contract_name,
                contract_discount_percentage, contract_type,
                payment_method, payment_status,
                amount_paid, change_amount, payment_reference,
                split_payment_details,
                insurance_preauth_number, insurance_claim_number,
                patient_copay_amount, insurance_covered_amount,
                insurance_verified,
                prescription_id, prescription_number,
                prescriber_name, prescriber_license,
                cashier_id, pharmacist_id,
                status, notes,
                receipt_printed, receipt_emailed,
                sync_version, sync_status,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:org_id AS UUID),
                CAST(:branch_id AS UUID), :sale_number,
                CAST(:customer_id AS UUID), :customer_name,
                :subtotal, :discount_amount, :tax_amount, :total_amount,
                CAST(:price_contract_id AS UUID), :contract_name,
                :contract_discount_pct, :contract_type,
                :payment_method, :payment_status,
                :amount_paid, :change_amount, :payment_reference,
                CAST(:split_payment_details AS JSONB),
                :insurance_preauth_number, :insurance_claim_number,
                :patient_copay_amount, :insurance_covered_amount,
                :insurance_verified,
                CAST(:prescription_id AS UUID), :prescription_number,
                :prescriber_name, :prescriber_license,
                CAST(:cashier_id AS UUID), CAST(:pharmacist_id AS UUID),
                :status, :notes,
                FALSE, FALSE,
                1, 'synced',
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        """),
        {
            "id": sale_id,
            "org_id": org_id,
            "branch_id": branch_id,
            "sale_number": payload["sale_number"],
            "customer_id": _uuid_str_or_none(payload.get("customer_id")),
            "customer_name": payload.get("customer_name"),
            "subtotal": _decimal_str(payload.get("subtotal", 0)),
            "discount_amount": _decimal_str(payload.get("discount_amount", 0)),
            "tax_amount": _decimal_str(payload.get("tax_amount", 0)),
            "total_amount": _decimal_str(payload.get("total_amount", 0)),
            "price_contract_id": _uuid_str_or_none(payload.get("price_contract_id")),
            "contract_name": payload.get("contract_name"),
            "contract_discount_pct": _decimal_str_or_none(
                payload.get("contract_discount_percentage")
            ),
            "contract_type": payload.get("contract_type"),
            "payment_method": payload["payment_method"],
            "payment_status": payload.get("payment_status", "completed"),
            "amount_paid": _decimal_str_or_none(payload.get("amount_paid")),
            "change_amount": _decimal_str_or_none(payload.get("change_amount")),
            "payment_reference": payload.get("payment_reference"),
            "split_payment_details": _json_or_none(
                payload.get("split_payment_details")
            ),
            "insurance_preauth_number": payload.get("insurance_preauth_number"),
            "insurance_claim_number": payload.get("insurance_claim_number"),
            "patient_copay_amount": _decimal_str_or_none(
                payload.get("patient_copay_amount")
            ),
            "insurance_covered_amount": _decimal_str_or_none(
                payload.get("insurance_covered_amount")
            ),
            "insurance_verified": bool(payload.get("insurance_verified", False)),
            "prescription_id": _uuid_str_or_none(payload.get("prescription_id")),
            "prescription_number": payload.get("prescription_number"),
            "prescriber_name": payload.get("prescriber_name"),
            "prescriber_license": payload.get("prescriber_license"),
            "cashier_id": str(payload["cashier_id"]),
            "pharmacist_id": _uuid_str_or_none(payload.get("pharmacist_id")),
            "status": payload.get("status", "completed"),
            "notes": payload.get("notes"),
            "created_at": event.authored_at,
            "updated_at": event.authored_at,
        },
    )
    if result.fetchone() is None:
        # Sale already projected — skip all side effects (idempotent).
        return

    # ── 2. Process items: insert line items, allocations, deduct stock ────────
    items: List[Dict[str, Any]] = payload.get("items") or []
    sale_number = payload["sale_number"]
    cashier_id = str(payload["cashier_id"])

    for item in items:
        await _apply_item(
            db=db,
            item=item,
            sale_id=sale_id,
            org_id=org_id,
            branch_id=branch_id,
            sale_number=sale_number,
            cashier_id=cashier_id,
            authored_at=event.authored_at,
            now=now,
        )


async def _apply_item(
    db: AsyncSession,
    item: Dict[str, Any],
    sale_id: str,
    org_id: str,
    branch_id: str,
    sale_number: str,
    cashier_id: str,
    authored_at: datetime,
    now: datetime,
) -> None:
    item_id = str(item["item_id"])
    drug_id = str(item["drug_id"])
    item_qty = int(item["quantity"])
    allocations: List[Dict[str, Any]] = item.get("batch_allocations") or []

    # Derive the primary batch_id: first allocation's batch_id (mirrors the
    # existing service's behaviour of tagging the item with its primary batch).
    primary_batch_id = (
        _uuid_str_or_none(allocations[0].get("batch_id")) if allocations else None
    )

    # ── 2a. INSERT sale_item ──────────────────────────────────────────────────
    await db.execute(
        text("""
            INSERT INTO sale_items (
                id, sale_id, drug_id, drug_name, drug_sku,
                batch_id,
                quantity, refunded_quantity,
                unit_price, subtotal,
                discount_percentage, discount_amount,
                tax_rate, tax_amount, total_price,
                requires_prescription, prescription_verified,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS UUID), CAST(:sale_id AS UUID),
                CAST(:drug_id AS UUID), :drug_name, :drug_sku,
                CAST(:batch_id AS UUID),
                :quantity, 0,
                :unit_price, :subtotal,
                :discount_pct, :discount_amount,
                :tax_rate, :tax_amount, :total_price,
                :requires_rx, :rx_verified,
                :created_at, :updated_at
            )
            ON CONFLICT (id) DO NOTHING
        """),
        {
            "id": item_id,
            "sale_id": sale_id,
            "drug_id": drug_id,
            "drug_name": item["drug_name"],
            "drug_sku": item.get("drug_sku"),
            "batch_id": primary_batch_id,
            "quantity": item_qty,
            "unit_price": _decimal_str(item.get("unit_price", 0)),
            "subtotal": _decimal_str(item.get("subtotal", 0)),
            "discount_pct": _decimal_str(item.get("discount_percentage", 0)),
            "discount_amount": _decimal_str(item.get("discount_amount", 0)),
            "tax_rate": _decimal_str(item.get("tax_rate", 0)),
            "tax_amount": _decimal_str(item.get("tax_amount", 0)),
            "total_price": _decimal_str(item.get("total_price", 0)),
            "requires_rx": bool(item.get("requires_prescription", False)),
            "rx_verified": bool(item.get("prescription_verified", False)),
            "created_at": authored_at,
            "updated_at": authored_at,
        },
    )

    if not allocations:
        # Draft sale or no batch tracking — no inventory side effects.
        return

    # ── 2b. INSERT batch allocations + UPDATE drug_batches ───────────────────
    # Collect (alloc, batch_qty_before, batch_qty_after) for movement inserts.
    batch_results: List[Tuple[Dict[str, Any], int, int]] = []
    total_deducted = 0

    for alloc in allocations:
        alloc_id = str(alloc["allocation_id"])
        alloc_batch_id = _uuid_str_or_none(alloc.get("batch_id"))
        alloc_qty = int(alloc["quantity"])
        total_deducted += alloc_qty

        await db.execute(
            text("""
                INSERT INTO sale_item_batch_allocations (
                    id, sale_item_id, branch_id, drug_id, batch_id,
                    batch_number, batch_expiry_date,
                    quantity, refunded_quantity,
                    unit_cost_at_sale, unit_price_at_sale,
                    created_at, updated_at
                ) VALUES (
                    CAST(:id AS UUID), CAST(:sale_item_id AS UUID),
                    CAST(:branch_id AS UUID), CAST(:drug_id AS UUID),
                    CAST(:batch_id AS UUID),
                    :batch_number, :batch_expiry_date,
                    :quantity, 0,
                    :unit_cost_at_sale, :unit_price_at_sale,
                    :created_at, :updated_at
                )
                ON CONFLICT (id) DO NOTHING
            """),
            {
                "id": alloc_id,
                "sale_item_id": item_id,
                "branch_id": branch_id,
                "drug_id": drug_id,
                "batch_id": alloc_batch_id,
                "batch_number": alloc.get("batch_number"),
                "batch_expiry_date": _parse_date(alloc.get("batch_expiry_date")),
                "quantity": alloc_qty,
                "unit_cost_at_sale": _decimal_str_or_none(
                    alloc.get("unit_cost_at_sale")
                ),
                "unit_price_at_sale": _decimal_str_or_none(
                    alloc.get("unit_price_at_sale")
                ),
                "created_at": now,
                "updated_at": now,
            },
        )

        if alloc_batch_id:
            # The expiry guard is evaluated against `authored_at` — when the
            # cashier actually made the sale — not against the projection time.
            # An offline sale is legitimate if the batch was in date when it was
            # rung up, even if it syncs days later and the batch has since
            # expired. Without this predicate the client alone decided which
            # batch to draw from, so a terminal holding a stale view could push
            # an expired-batch sale straight through; the online path has always
            # filtered on expiry (see SalesService FEFO allocation).
            batch_row = (
                await db.execute(
                    text("""
                        UPDATE drug_batches
                           SET remaining_quantity = remaining_quantity - :qty,
                               updated_at = :now,
                               sync_version = sync_version + 1,
                               sync_status = 'synced'
                         WHERE id = :batch_id
                           AND branch_id = CAST(:branch_id AS UUID)
                           AND drug_id   = CAST(:drug_id AS UUID)
                           AND remaining_quantity >= :qty
                           AND (
                                 expiry_date IS NULL
                                 OR expiry_date >= CAST(:sold_at AS DATE)
                               )
                        RETURNING
                            remaining_quantity + :qty AS qty_before,
                            remaining_quantity       AS qty_after
                    """),
                    {
                        "batch_id": alloc_batch_id,
                        "branch_id": branch_id,
                        "drug_id": drug_id,
                        "qty": alloc_qty,
                        "now": now,
                        "sold_at": authored_at,
                    },
                )
            ).fetchone()

            if batch_row is None:
                # Distinguish the two causes so the dead-letter entry tells a
                # human which one it was rather than always blaming stock.
                diag = (
                    await db.execute(
                        text("""
                            SELECT remaining_quantity, expiry_date
                              FROM drug_batches
                             WHERE id = :batch_id
                               AND branch_id = CAST(:branch_id AS UUID)
                               AND drug_id   = CAST(:drug_id AS UUID)
                        """),
                        {"batch_id": alloc_batch_id, "branch_id": branch_id, "drug_id": drug_id},
                    )
                ).fetchone()

                if diag is None:
                    raise ValueError(
                        f"Unknown batch {alloc_batch_id} for drug {drug_id}"
                    )
                if (
                    diag.expiry_date is not None
                    and diag.expiry_date < authored_at.date()
                ):
                    raise ValueError(
                        f"Expired batch {alloc_batch_id} (expired "
                        f"{diag.expiry_date}) cannot be sold: sale was authored "
                        f"{authored_at.date()} (drug {drug_id})"
                    )
                raise ValueError(
                    f"Insufficient stock in batch {alloc_batch_id}: "
                    f"need {alloc_qty} units, have {diag.remaining_quantity} "
                    f"(drug {drug_id})"
                )
            batch_results.append((alloc, batch_row.qty_before, batch_row.qty_after))

    # ── 2c. UPDATE branch_inventory (total for this drug) ────────────────────
    inv_row = (
        await db.execute(
            text("""
                UPDATE branch_inventory
                   SET quantity = quantity - :qty,
                       updated_at = :now,
                       sync_version = sync_version + 1,
                       sync_status = 'synced'
                 WHERE id = (
                           SELECT id
                             FROM branch_inventory
                            WHERE branch_id = :branch_id
                              AND drug_id   = :drug_id
                         ORDER BY id
                            LIMIT 1
                       )
                   AND quantity >= :qty
                RETURNING
                    quantity + :qty AS qty_before,
                    quantity        AS qty_after
            """),
            {
                "qty": total_deducted,
                "branch_id": branch_id,
                "drug_id": drug_id,
                "now": now,
            },
        )
    ).fetchone()

    if inv_row is None:
        raise ValueError(
            f"Insufficient branch_inventory for drug {drug_id} in branch {branch_id} "
            f"(need {total_deducted} units)"
        )

    # ── 2d. INSERT inventory_movements (one per batch allocation) ────────────
    running_branch = inv_row.qty_before
    unit_price_str = _decimal_str_or_none(item.get("unit_price"))

    for alloc, batch_before, batch_after in batch_results:
        alloc_qty = int(alloc["quantity"])
        branch_before = running_branch
        branch_after = running_branch - alloc_qty
        running_branch = branch_after

        await db.execute(
            text("""
                INSERT INTO inventory_movements (
                    id, organization_id, branch_id, drug_id, batch_id,
                    movement_type, quantity_change,
                    quantity_before, quantity_after,
                    batch_quantity_before, batch_quantity_after,
                    unit_price,
                    source_type, source_id, source_line_id,
                    reference_number, reason,
                    created_by, occurred_at
                ) VALUES (
                    CAST(:id AS UUID),
                    CAST(:org_id AS UUID),
                    CAST(:branch_id AS UUID),
                    CAST(:drug_id AS UUID),
                    CAST(:batch_id AS UUID),
                    'sale', :qty_change,
                    :branch_before, :branch_after,
                    :batch_before, :batch_after,
                    :unit_price,
                    'sale',
                    CAST(:source_id AS UUID),
                    CAST(:source_line_id AS UUID),
                    :ref_number, :reason,
                    CAST(:created_by AS UUID), :occurred_at
                )
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "branch_id": branch_id,
                "drug_id": drug_id,
                "batch_id": _uuid_str_or_none(alloc.get("batch_id")),
                "qty_change": -alloc_qty,
                "branch_before": branch_before,
                "branch_after": branch_after,
                "batch_before": batch_before,
                "batch_after": batch_after,
                "unit_price": unit_price_str,
                "source_id": sale_id,
                "source_line_id": item_id,
                "ref_number": sale_number,
                "reason": f"Sale {sale_number}",
                "created_by": cashier_id,
                "occurred_at": authored_at,
            },
        )

    # ── 2e. INSERT stock_adjustment (one per item/drug) ───────────────────────
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
                'correction', :qty_change,
                :prev_qty, :new_qty,
                :reason,
                CAST(:adjusted_by AS UUID),
                :created_at, :updated_at
            )
        """),
        {
            "id": str(uuid.uuid4()),
            "branch_id": branch_id,
            "drug_id": drug_id,
            "qty_change": -total_deducted,
            "prev_qty": inv_row.qty_before,
            "new_qty": inv_row.qty_after,
            "reason": f"Sale {sale_number}",
            "adjusted_by": cashier_id,
            "created_at": now,
            "updated_at": now,
        },
    )


async def _apply_voided(event: EventEnvelope, db: AsyncSession) -> None:
    payload = event.payload
    now = datetime.now(timezone.utc)
    sale_id = str(event.aggregate_id)
    org_id = str(event.org_id)

    sale_row = (
        await db.execute(
            text("""
                SELECT status, branch_id, sale_number, cashier_id
                  FROM sales
                 WHERE id = :id
                   AND organization_id = :org_id
            """),
            {"id": sale_id, "org_id": org_id},
        )
    ).fetchone()

    if sale_row is None or sale_row.status == "cancelled":
        return  # idempotent

    branch_id = str(sale_row.branch_id)
    sale_number = sale_row.sale_number
    voided_by = _uuid_str_or_none(payload.get("voided_by")) or str(sale_row.cashier_id)
    void_reason = payload.get("void_reason")

    # Load all batch allocations for this sale to drive stock restoration.
    alloc_rows = (
        await db.execute(
            text("""
                SELECT a.id       AS alloc_id,
                       a.batch_id,
                       a.drug_id,
                       a.quantity,
                       a.sale_item_id
                  FROM sale_item_batch_allocations a
                  JOIN sale_items si ON si.id = a.sale_item_id
                 WHERE si.sale_id = :sale_id
            """),
            {"sale_id": sale_id},
        )
    ).fetchall()

    for alloc in alloc_rows:
        drug_id = str(alloc.drug_id)
        alloc_qty = int(alloc.quantity)

        # Restore drug_batch remaining_quantity
        if alloc.batch_id is not None:
            batch_row = (
                await db.execute(
                    text("""
                        UPDATE drug_batches
                           SET remaining_quantity = remaining_quantity + :qty,
                               updated_at = :now,
                               sync_version = sync_version + 1,
                               sync_status = 'synced'
                         WHERE id = :batch_id
                           AND branch_id = CAST(:branch_id AS UUID)
                           AND drug_id   = CAST(:drug_id AS UUID)
                        RETURNING
                            remaining_quantity - :qty AS qty_before,
                            remaining_quantity        AS qty_after
                    """),
                    {"batch_id": str(alloc.batch_id), "branch_id": branch_id, "drug_id": drug_id, "qty": alloc_qty, "now": now},
                )
            ).fetchone()

            if batch_row:
                await db.execute(
                    text("""
                        INSERT INTO inventory_movements (
                            id, organization_id, branch_id, drug_id, batch_id,
                            movement_type, quantity_change,
                            quantity_before, quantity_after,
                            batch_quantity_before, batch_quantity_after,
                            source_type, source_id, source_line_id,
                            reference_number, reason,
                            created_by, occurred_at
                        ) VALUES (
                            CAST(:id AS UUID),
                            CAST(:org_id AS UUID),
                            CAST(:branch_id AS UUID),
                            CAST(:drug_id AS UUID),
                            CAST(:batch_id AS UUID),
                            'refund', :qty_change,
                            :branch_before, :branch_after,
                            :batch_before, :batch_after,
                            'sale',
                            CAST(:source_id AS UUID),
                            CAST(:source_line_id AS UUID),
                            :ref_number, :reason,
                            CAST(:created_by AS UUID), :occurred_at
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "org_id": org_id,
                        "branch_id": branch_id,
                        "drug_id": drug_id,
                        "batch_id": str(alloc.batch_id),
                        "qty_change": alloc_qty,
                        # Branch-level before/after filled in below after inv update.
                        # Use batch quantities here; branch values require an extra
                        # SELECT that we avoid for simplicity in the void path.
                        "branch_before": 0,
                        "branch_after": alloc_qty,
                        "batch_before": batch_row.qty_before,
                        "batch_after": batch_row.qty_after,
                        "source_id": sale_id,
                        "source_line_id": str(alloc.sale_item_id),
                        "ref_number": sale_number,
                        "reason": f"Sale void: {sale_number}",
                        "created_by": voided_by,
                        "occurred_at": event.authored_at,
                    },
                )

        # Restore branch_inventory quantity
        inv_row = (
            await db.execute(
                text("""
                    UPDATE branch_inventory
                       SET quantity = quantity + :qty,
                           updated_at = :now,
                           sync_version = sync_version + 1,
                           sync_status = 'synced'
                     WHERE id = (
                               SELECT id
                                 FROM branch_inventory
                                WHERE branch_id = :branch_id
                                  AND drug_id   = :drug_id
                             ORDER BY id
                                LIMIT 1
                           )
                    RETURNING
                        quantity - :qty AS qty_before,
                        quantity        AS qty_after
                """),
                {"qty": alloc_qty, "branch_id": branch_id, "drug_id": drug_id, "now": now},
            )
        ).fetchone()

        if inv_row:
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
                        'return', :qty_change,
                        :prev_qty, :new_qty,
                        :reason,
                        CAST(:adjusted_by AS UUID),
                        :created_at, :updated_at
                    )
                """),
                {
                    "id": str(uuid.uuid4()),
                    "branch_id": branch_id,
                    "drug_id": drug_id,
                    "qty_change": alloc_qty,
                    "prev_qty": inv_row.qty_before,
                    "new_qty": inv_row.qty_after,
                    "reason": f"Sale void: {sale_number}",
                    "adjusted_by": voided_by,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    # Mark the sale cancelled.
    await db.execute(
        text("""
            UPDATE sales
               SET status = 'cancelled',
                   cancelled_at = :cancelled_at,
                   cancelled_by = :cancelled_by,
                   cancellation_reason = :reason,
                   updated_at = :updated_at,
                   sync_version = sync_version + 1,
                   sync_status = 'synced'
             WHERE id = :id
               AND organization_id = :org_id
               AND status != 'cancelled'
        """),
        {
            "id": sale_id,
            "org_id": org_id,
            "cancelled_at": event.authored_at,
            "cancelled_by": voided_by,
            "reason": void_reason,
            "updated_at": now,
        },
    )


# ── small conversions ────────────────────────────────────────────────────────


def _uuid_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _decimal_str(v: Any) -> str:
    if v is None:
        return "0"
    return str(v)


def _decimal_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _json_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v)


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
