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
from app.services.sales.utils.loyalty import (
    apply_loyalty_award,
    loyalty_payload,
)
from app.services.sync.eventlog.loyalty_event_id import (
    customer_loyalty_changed_event_id,
)
from app.services.sync.eventlog.stock_emitter import StockEventEmitter
from app.services.sync.eventlog.projector import (
    Projector,
    ProjectorRegistry,
    ProjectorResult,
    ProjectorStatus,
)
from app.services.sync.eventlog.projectors._fefo import fefo_allocate
from app.services.sync.eventlog.refill_event_id import (
    prescription_refill_used_event_id,
)
from app.services.sync.eventlog.stock_emitter import (
    StockEventEmitter,
    _coerce_uuid,
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
                :id, :org_id,
                :branch_id, :sale_number,
                :customer_id, :customer_name,
                :subtotal, :discount_amount, :tax_amount, :total_amount,
                :price_contract_id, :contract_name,
                :contract_discount_pct, :contract_type,
                :payment_method, :payment_status,
                :amount_paid, :change_amount, :payment_reference,
                :split_payment_details,
                :insurance_preauth_number, :insurance_claim_number,
                :patient_copay_amount, :insurance_covered_amount,
                :insurance_verified,
                :prescription_id, :prescription_number,
                :prescriber_name, :prescriber_license,
                :cashier_id, :pharmacist_id,
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

    sync_version = int(payload.get("sync_protocol_version", 1))
    use_server_fefo = sync_version >= 2
    terminal_id: Optional[str] = payload.get("terminal_id") or None

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
            use_server_fefo=use_server_fefo,
            terminal_id=terminal_id,
        )

    # ── 3. Consume a prescription refill ─────────────────────────────────────
    # Placed after the `RETURNING id` gate above, so it runs exactly once per
    # sale id: a replayed sale_created finds the existing sale row, returns None,
    # and leaves before reaching this. That is the idempotency this step needs —
    # the event id alone would not be enough, because the emit and the decrement
    # have to happen together or not at all.
    await _apply_prescription_refill(
        db=db,
        event=event,
        sale_id=sale_id,
        org_id=org_id,
        branch_id=branch_id,
        payload=payload,
    )

    # ── 4. Award loyalty for the offline sale ───────────────────────────────
    # Placed after the `RETURNING id` gate for the same reason as the refill: a
    # replayed sale_created finds the existing row and returns None before
    # reaching here, so the award and the emit happen together or not at all.
    await _apply_offline_loyalty(
        db=db,
        event=event,
        sale_id=sale_id,
        org_id=org_id,
        branch_id=branch_id,
        payload=payload,
    )


async def _apply_offline_loyalty(
    *,
    db: AsyncSession,
    event: EventEnvelope,
    sale_id: str,
    org_id: str,
    branch_id: str,
    payload: Dict[str, Any],
) -> None:
    """Award loyalty for an offline-synced sale, then announce it.

    Before this, an offline sale produced a `sales` row with no points behind it:
    the only award code path was `SalesService.process_sale`, which an offline
    device never calls. Mike's single offline sale of 5 is the production
    instance - he has been at 0 points ever since.

    The award goes through the SAME helper the online path uses
    (`app/services/sales/utils/loyalty.apply_loyalty_award`), so points_per_unit
    and the post-discount total cannot drift between the two paths.

    NEVER REJECTS. Loyalty is a consequence of a sale that already happened; the
    medicine has left the shelf and refusing the event would not undo it. A
    failure here is logged and swallowed so a loyalty problem can never cost the
    pharmacy a sale record.

    Walk-in sales earn nothing: they carry no customer_id. Loyalty disabled at the
    org earns nothing. Re-projecting an existing sale changes nothing, because the
    `RETURNING id` gate above returns before this is ever reached.
    """
    customer_id = _uuid_str_or_none(payload.get("customer_id"))
    if not customer_id:
        return  # walk-in

    try:
        customer_row = (
            await db.execute(
                text(
                    """
                    SELECT c.id, c.loyalty_points, c.loyalty_tier,
                           c.total_orders, c.total_value, c.customer_type,
                           o.settings::json AS settings
                      FROM customers c
                      JOIN organizations o ON o.id = c.organization_id
                     WHERE c.id = :customer_id AND c.organization_id = :org_id
                     FOR UPDATE OF c
                    """
                ),
                {"customer_id": customer_id, "org_id": org_id},
            )
        ).first()
        if customer_row is None:
            return

        # Only a registered profile earns. A walk_in row is a till artefact with
        # nobody to credit; the online path treats those the same way.
        if customer_row.customer_type == "walk_in":
            return

        org = _SimpleOrg(settings=customer_row.settings)
        customer = _SimpleCustomer(
            loyalty_points=customer_row.loyalty_points,
            loyalty_tier=customer_row.loyalty_tier,
            total_orders=customer_row.total_orders,
            total_value=customer_row.total_value,
        )

        # The post-discount total is what the online path awards on.
        total_amount = payload.get("total_amount")
        if total_amount in (None, ""):
            subtotal = _decimal_or_zero(payload.get("subtotal"))
            discount = _decimal_or_zero(payload.get("discount_amount"))
            total_amount = float(subtotal - discount)

        award = await apply_loyalty_award(
            db,
            customer,
            org,
            sale_id=sale_id,
            direction="earn",
            total_amount=total_amount,
            increment_orders=True,
        )
        if award is None:
            return

        await db.execute(
            text(
                """
                UPDATE customers
                   SET loyalty_points = :points,
                       loyalty_tier   = :tier,
                       total_orders   = :orders,
                       total_value    = :value,
                       updated_at     = now()
                 WHERE id = :customer_id AND organization_id = :org_id
                """
            ),
            {
                "points": award.loyalty_points,
                "tier": award.loyalty_tier,
                "orders": customer.total_orders,
                "value": customer.total_value,
                "customer_id": customer_id,
                "org_id": org_id,
            },
        )

        await StockEventEmitter.emit_in_transaction(
            db,
            org_id=uuid.UUID(org_id),
            event_type="customer_loyalty_changed",
            aggregate_type=AggregateType.CUSTOMER,
            aggregate_id=uuid.UUID(customer_id),
            # Derived, never random: re-projecting the same sale derives the same
            # id, so the append returns ALREADY_APPENDED and a duplicate is free.
            event_id=customer_loyalty_changed_event_id(customer_id, sale_id, "earn"),
            payload=loyalty_payload(
                award,
                customer_id=customer_id,
                sale_id=sale_id,
                direction="earn",
                organization_id=org_id,
                sale_number=payload.get("sale_number"),
                source="offline_sale",
            ),
            authored_by=event.authored_by,
            branch_id=uuid.UUID(branch_id) if branch_id else None,
        )
    except Exception as exc:  # noqa: BLE001 - a sale must never fail on loyalty
        logger.warning(
            "Loyalty award skipped for offline sale %s (customer %s): %s",
            sale_id,
            customer_id,
            exc,
            exc_info=True,
        )


async def _apply_prescription_refill(
    *,
    db: AsyncSession,
    event: EventEnvelope,
    sale_id: str,
    org_id: str,
    branch_id: str,
    payload: Dict[str, Any],
) -> None:
    """Consume one refill for an offline-synced sale, then announce it.

    Before this, an offline dispense decremented the DEVICE's counter only. The
    server's counter never moved, so (a) the Prescriptions page read the server
    and showed the prescription untouched, and (b) with two devices the server's
    guard could not stop a second dispense of the same prescription, because the
    server had never counted the first one.

    NEVER REJECTS. The medicine has already left the shelf; the physical sale is
    not reversible by refusing an event. So the counter clamps at 0 (respecting
    `check_refills_remaining`) and the over-dispense is recorded loudly instead
    of silently accepted. A non-active status is likewise not a reason to drop
    the event — a prescription cancelled on another device while this one was
    offline still got dispensed, and that is worth seeing.
    """
    rx_id = _uuid_str_or_none(payload.get("prescription_id"))
    if not rx_id:
        return

    row = (
        await db.execute(
            text(
                """
                SELECT id, status, refills_allowed, refills_remaining,
                       customer_id
                  FROM prescriptions
                 WHERE id = :rx_id AND organization_id = :org_id
                 FOR UPDATE
                """
            ),
            {"rx_id": rx_id, "org_id": org_id},
        )
    ).mappings().first()

    if row is None:
        # The prescription was never pushed to the server, or belongs to another
        # org. Nothing to decrement. Logged rather than raised: see the docstring.
        logger.warning(
            "sale_created references a prescription the server does not have: "
            "sale_id=%s prescription_id=%s org_id=%s",
            sale_id, rx_id, org_id,
        )
        return

    # `pharmacist_id` is the person who verified the prescription. An offline sale
    # may not have one (the device's pre-flight does not require a pharmacist),
    # and the cashier is NOT a substitute — `verified_by` means "pharmacist who
    # verified", so writing a cashier id there would assert something untrue about
    # a controlled-drug record. So: use the pharmacist when the device captured
    # one, otherwise leave it NULL and let `verified_at` carry the timestamp.
    pharmacist_id = _uuid_str_or_none(payload.get("pharmacist_id"))
    refill_date = event.authored_at.date()
    verified_at = event.authored_at

    before_status = row["status"]
    before_remaining = int(row["refills_remaining"])
    after_remaining = max(0, before_remaining - 1)
    over_dispensed = before_remaining <= 0 or before_status != "active"
    after_status = "filled" if after_remaining == 0 else "active"

    await db.execute(
        text(
            """
            UPDATE prescriptions
               SET refills_remaining = :after_remaining,
                   last_refill_date  = :refill_date,
                   status            = :after_status,
                   verified_by       = :verified_by,
                   verified_at       = :verified_at,
                   updated_at        = :updated_at
             WHERE id = :rx_id AND organization_id = :org_id
            """
        ),
        {
            "after_remaining": after_remaining,
            "refill_date": refill_date,
            "after_status": after_status,
            "verified_by": pharmacist_id,
            "verified_at": verified_at,
            "updated_at": verified_at,
            "rx_id": rx_id,
            "org_id": org_id,
        },
    )

    if over_dispensed:
        # Recorded in three places on purpose: the structured log for alerting,
        # the event payload so devices and the audit trail can see it, and
        # system_alerts is deliberately NOT written here — this runs inside the
        # ingest transaction and a monitor has never been asked to own it.
        logger.error(
            "OVER-DISPENSE: prescription refilled while not fillable. "
            "sale_id=%s prescription_id=%s org_id=%s branch_id=%s "
            "status_before=%s refills_before=%d refills_after=%d "
            "pharmacist_id=%s",
            sale_id, rx_id, org_id, branch_id,
            before_status, before_remaining, after_remaining, pharmacist_id,
        )

    await StockEventEmitter.emit_in_transaction(
        db,
        org_id=_coerce_uuid(org_id),
        event_type="prescription_refill_used",
        aggregate_type=AggregateType.PRESCRIPTION,
        aggregate_id=_coerce_uuid(rx_id),
        # Derived from the sale id: a replayed sale_created never reaches here
        # (the RETURNING gate returns first), and if it somehow did, the same id
        # makes the append a no-op.
        event_id=prescription_refill_used_event_id(rx_id, sale_id),
        payload={
            "prescription_id": rx_id,
            "sale_id": sale_id,
            "sale_number": payload.get("sale_number"),
            "organization_id": org_id,
            "branch_id": branch_id,
            # Absolute post-state, so a device converges on the server's number
            # rather than applying its own delta on top.
            "refills_remaining": after_remaining,
            "refills_allowed": int(row["refills_allowed"]),
            "status": after_status,
            "last_refill_date": refill_date.isoformat(),
            "verified_by": pharmacist_id,
            "verified_at": verified_at.isoformat(),
            "source": "offline_sale_sync",
            "over_dispensed": over_dispensed,
            "status_before": before_status,
            "refills_before": before_remaining,
        },
        authored_by=(
            _coerce_uuid(pharmacist_id)
            if pharmacist_id
            else _coerce_uuid(payload.get("cashier_id"))
        ),
        branch_id=_coerce_uuid(branch_id) if branch_id else None,
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
    use_server_fefo: bool = False,
    terminal_id: Optional[str] = None,
) -> None:
    item_id = str(item["item_id"])
    drug_id = str(item["drug_id"])
    item_qty = int(item["quantity"])

    if use_server_fefo:
        try:
            server_allocations = await fefo_allocate(
                db=db, branch_id=branch_id, drug_id=drug_id,
                quantity=item_qty, authored_at=authored_at,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        allocations = [
            {
                "allocation_id": str(uuid.uuid4()),
                "batch_id": a.batch_id,
                "batch_number": a.batch_number,
                "batch_expiry_date": a.expiry_date.isoformat() if hasattr(a.expiry_date, 'isoformat') else a.expiry_date,
                "quantity": a.quantity,
                "unit_cost_at_sale": a.unit_cost,
                "unit_price_at_sale": a.unit_price,
            }
            for a in server_allocations
        ]
        provisional = item.get("provisional_batch_allocations") or item.get("batch_allocations") or []
    else:
        allocations = item.get("batch_allocations") or []
        provisional = []

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
                :id, :sale_id,
                :drug_id, :drug_name, :drug_sku,
                :batch_id,
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
                    :id, :sale_item_id,
                    :branch_id, :drug_id,
                    :batch_id,
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
                           AND branch_id = :branch_id
                           AND drug_id   = :drug_id
                           AND remaining_quantity >= :qty
                           AND (
                                 expiry_date IS NULL
                                 OR expiry_date >= :sold_at
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
                               AND branch_id = :branch_id
                               AND drug_id   = :drug_id
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
                    :id,
                    :org_id,
                    :branch_id,
                    :drug_id,
                    :batch_id,
                    'sale', :qty_change,
                    :branch_before, :branch_after,
                    :batch_before, :batch_after,
                    :unit_price,
                    'sale',
                    :source_id,
                    :source_line_id,
                    :ref_number, :reason,
                    :created_by, :occurred_at
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
                :id,
                :branch_id,
                :drug_id,
                'correction', :qty_change,
                :prev_qty, :new_qty,
                :reason,
                :adjusted_by,
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

    # ── 2f. Increment lease consumed_quantity for this terminal ───────────────
    # If the client included terminal_id in the payload, find any active lease
    # for (branch, drug, terminal) and mark the quantity as consumed.
    # This keeps server-side lease accounting in sync with actual sales.
    if terminal_id:
        await db.execute(
            text("""
                UPDATE stock_leases
                   SET consumed_quantity = LEAST(
                           consumed_quantity + :qty,
                           leased_quantity
                       ),
                       updated_at = :now
                 WHERE branch_id  = CAST(:branch_id AS UUID)
                   AND drug_id    = CAST(:drug_id AS UUID)
                   AND terminal_id = :terminal_id
                   AND status      = 'active'
                   AND expires_at  > :now
            """),
            {
                "qty": total_deducted,
                "branch_id": branch_id,
                "drug_id": drug_id,
                "terminal_id": terminal_id,
                "now": now,
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
                           AND branch_id = :branch_id
                           AND drug_id   = :drug_id
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
                            :id,
                            :org_id,
                            :branch_id,
                            :drug_id,
                            :batch_id,
                            'refund', :qty_change,
                            :branch_before, :branch_after,
                            :batch_before, :batch_after,
                            'sale',
                            :source_id,
                            :source_line_id,
                            :ref_number, :reason,
                            :created_by, :occurred_at
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
                        :id,
                        :branch_id,
                        :drug_id,
                        'return', :qty_change,
                        :prev_qty, :new_qty,
                        :reason,
                        :adjusted_by,
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


class _SimpleOrg:
    """Minimal stand-in: `apply_loyalty_award` only reads `.settings`."""

    __slots__ = ("settings",)

    def __init__(self, settings: Any) -> None:
        self.settings = settings if isinstance(settings, dict) else {}


class _SimpleCustomer:
    """Minimal stand-in for the loyalty helper's mutations.

    A plain object rather than the ORM row: the row was read with `FOR UPDATE`
    through raw SQL to keep the lock, and writing it back is one explicit UPDATE
    below. Using the ORM object here would open a second, unlocked read.
    """

    __slots__ = ("loyalty_points", "loyalty_tier", "total_orders", "total_value")

    def __init__(
        self,
        loyalty_points: Any,
        loyalty_tier: Any,
        total_orders: Any,
        total_value: Any,
    ) -> None:
        self.loyalty_points = int(loyalty_points or 0)
        self.loyalty_tier = loyalty_tier
        self.total_orders = int(total_orders or 0)
        self.total_value = float(total_value or 0)


def _decimal_or_zero(v: Any):
    """Coerce a payload money value to Decimal, defaulting to 0."""
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(v if v not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


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
