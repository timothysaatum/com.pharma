"""
Sync Service
============
Handles pull (server → branch delta) and push (branch → server records).

Pull strategy
-------------
All table queries run inside a single REPEATABLE READ transaction that is
opened before any query is issued.  This prevents the "phantom read" data-loss
bug where a record committed between two sequential table queries has
``updated_at < sync_timestamp`` and is therefore missed by both the current
pull and every future pull.  The ``sync_timestamp`` returned to the client is
the server-side ``now`` captured at transaction open — not after queries.

Pull filtering
--------------
Sales (and other branch-owned records) are only pulled when their
``sync_status`` is ``'synced'``.  Records still marked ``'pending'`` on the
server were created via the POS endpoint but not yet flushed to synced state;
pulling them would cause the client to immediately try to push them back,
creating an infinite sync loop.

Push strategy
-------------
Each record is processed inside its own ``begin_nested()`` savepoint so a
DB-level error or conflict on record N does not roll back the records already
accepted for records 1..N-1.  A single ``db.commit()`` is issued after the
entire batch has been processed.

Conflict resolution
-------------------
server_wins  — server record is newer; client must re-pull before re-pushing.
manual_required — (customers) risk of duplicates; client must resolve manually.

Field safety
------------
Every push handler explicitly whitelists the fields it will accept from the
client.  Attempting to push ``organization_id``, ``branch_id``, or other
ownership fields via the sync payload is silently ignored.

Null handling
-------------
``_clean`` only strips the three sync-metadata keys (``sync_status``,
``sync_hash``, ``last_synced_at``) that must not be written from client data.
It does NOT strip ``None`` values — intentional nulls (e.g. clearing
``effective_to``) must be preserved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.pricing.pricing_model import PriceContract
from app.models.sales.sales_model import Sale, PurchaseOrder
from app.schemas.customer_schemas import CustomerResponse
from app.schemas.drugs_schemas import DrugCategoryResponse, DrugResponse
from app.schemas.inventory_schemas import BranchInventoryResponse, DrugBatchResponse
from app.schemas.price_contract_schemas import PriceContractResponse
from app.schemas.purchase_order_schemas import PurchaseOrderResponse
from app.schemas.sales_schemas import SaleResponse
from app.schemas.sync_schemas import (
    PullRequest,
    PullResponse,
    PushConflict,
    PushRecord,
    PushRequest,
    PushResponse,
    PushResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conflict resolution rules per table
# ---------------------------------------------------------------------------
CONFLICT_RESOLUTION: Dict[str, str] = {
    "sales":             "server_wins",
    "branch_inventory":  "server_wins",
    "drug_batches":      "server_wins",
    "stock_adjustments": "server_wins",
    "purchase_orders":   "server_wins",
    "customers":         "manual_required",
}

# ---------------------------------------------------------------------------
# Sync-metadata keys that must never be written from client-supplied data.
# Only these are stripped; None values are preserved so intentional nulls
# (e.g. clearing effective_to back to NULL) are not silently dropped.
# ---------------------------------------------------------------------------
_SYNC_META_KEYS: frozenset[str] = frozenset(
    {"sync_status", "sync_hash", "last_synced_at"}
)

# ---------------------------------------------------------------------------
# Per-table field whitelists for push operations.
# Any key not in the whitelist is silently ignored.
# ---------------------------------------------------------------------------
_SALE_WRITABLE: frozenset[str] = frozenset({
    "id", "sale_number", "customer_id", "customer_name",
    "subtotal", "discount_amount", "tax_amount", "total_amount",
    # Schema aliases — client sends these; remapped to discount_amount before insert
    "total_discount_amount",
    "contract_discount_amount",
    "additional_discount_amount",
    "price_contract_id", "contract_name", "contract_discount_percentage",
    "payment_method", "payment_status", "amount_paid", "change_amount",
    "payment_reference",
    "insurance_claim_number", "patient_copay_amount", "insurance_covered_amount",
    "insurance_verified", "insurance_verified_at", "insurance_verified_by",
    "prescription_id", "prescription_number", "prescriber_name", "prescriber_license",
    "cashier_id", "pharmacist_id",
    "notes", "status",
    "receipt_printed", "receipt_emailed",
    "created_at", "updated_at",
})

_BATCH_WRITABLE: frozenset[str] = frozenset({
    "id", "drug_id", "batch_number",
    "quantity", "remaining_quantity",
    "manufacturing_date", "expiry_date",
    "cost_price", "selling_price",
    "supplier", "purchase_order_id",
    "created_at", "updated_at",
})

_ADJUSTMENT_WRITABLE: frozenset[str] = frozenset({
    "id", "drug_id",
    "adjustment_type", "quantity_change",
    "previous_quantity", "new_quantity",
    "reason",
    "transfer_to_branch_id",
    "created_at", "updated_at",
})

_INVENTORY_WRITABLE: frozenset[str] = frozenset({
    "id", "drug_id",
    "quantity", "reserved_quantity",
    "location", "selling_price",
    "updated_at",
})

_PO_WRITABLE: frozenset[str] = frozenset({
    "id", "po_number", "supplier_id",
    "subtotal", "tax_amount", "shipping_cost", "total_amount",
    "status",
    "expected_delivery_date", "received_date",
    "notes",
    "created_at", "updated_at",
})

_CUSTOMER_WRITABLE: frozenset[str] = frozenset({
    "id", "customer_type",
    "first_name", "last_name", "phone", "email", "date_of_birth", "address",
    "allergies", "chronic_conditions",
    "loyalty_points", "loyalty_tier",
    "preferred_contact_method", "marketing_consent",
    "insurance_provider_id", "insurance_member_id",
    "is_active",
    "created_at", "updated_at",
})


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strip sync-metadata keys that must not be written from client payloads.

    Intentional ``None`` values are preserved — this is by design so that
    fields like ``effective_to`` can be explicitly cleared to NULL via sync.
    """
    return {k: v for k, v in data.items() if k not in _SYNC_META_KEYS}


def _whitelist(data: Dict[str, Any], allowed: frozenset[str]) -> Dict[str, Any]:
    """Return only the keys present in ``allowed``, after stripping meta keys."""
    return {k: v for k, v in _clean(data).items() if k in allowed}


class SyncService:

    # =========================================================================
    # PULL
    # =========================================================================

    @staticmethod
    async def pull(
        db: AsyncSession,
        request: PullRequest,
        organization_id: uuid.UUID,
    ) -> PullResponse:
        """
        Return all records changed since ``request.last_sync_at``.

        All table queries execute within a single REPEATABLE READ transaction
        so every query sees the same consistent DB snapshot.  This prevents
        a record committed between two sequential table queries from being
        silently skipped in both the current pull and all future pulls.

        Branch-owned records (sales, inventory, batches) are filtered to
        ``sync_status = 'synced'`` so that server-created records that are
        still pending don't get pulled to the client and pushed right back,
        which would create an infinite sync loop.
        """
        branch_id = request.branch_id
        tables    = set(request.tables)

        # Capture now BEFORE opening the transaction so the client's next
        # last_sync_at is slightly behind the snapshot point, guaranteeing
        # no records fall in the gap.
        now   = datetime.now(timezone.utc)
        since = request.last_sync_at

        # Request a consistent snapshot so all table queries see the same DB state.
        # PostgreSQL: SET TRANSACTION ISOLATION LEVEL REPEATABLE READ
        # SQLite:     default isolation is already SERIALIZABLE (stronger than
        #             REPEATABLE READ) — the SET syntax does not exist and is not
        #             needed.  We catch OperationalError so the same code works in
        #             both development (SQLite/aiosqlite) and production (PostgreSQL).
        from sqlalchemy.exc import OperationalError as SA_OperationalError
        try:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        except SA_OperationalError:
            pass  # SQLite — default isolation is already sufficient

        result = PullResponse(sync_timestamp=now)
        total  = 0

        if "drugs" in tables:
            rows = await SyncService._pull_table(
                db, Drug, since, Drug.organization_id == organization_id
            )
            result.drugs = [DrugResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "drug_categories" in tables:
            rows = await SyncService._pull_table(
                db, DrugCategory, since,
                DrugCategory.organization_id == organization_id,
            )
            result.drug_categories = [DrugCategoryResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "price_contracts" in tables:
            rows = await SyncService._pull_table(
                db, PriceContract, since,
                PriceContract.organization_id == organization_id,
            )
            result.price_contracts = [PriceContractResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "customers" in tables:
            rows = await SyncService._pull_table(
                db, Customer, since,
                Customer.organization_id == organization_id,
            )
            result.customers = [CustomerResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "branch_inventory" in tables:
            rows = await SyncService._pull_table(
                db, BranchInventory, since,
                BranchInventory.branch_id == branch_id,
                # Only pull synced records — pending means the server hasn't
                # finished processing them; pulling them would cause the client
                # to push them straight back in a loop.
                BranchInventory.sync_status == "synced",
            )
            result.branch_inventory = [BranchInventoryResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "drug_batches" in tables:
            rows = await SyncService._pull_table(
                db, DrugBatch, since,
                DrugBatch.branch_id == branch_id,
                DrugBatch.sync_status == "synced",
            )
            result.drug_batches = [DrugBatchResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "sales" in tables:
            rows = await SyncService._pull_table(
                db, Sale, since,
                Sale.branch_id == branch_id,
                # IMPORTANT: only pull synced sales.  Sales created via the
                # POS endpoint are initially marked 'pending' by mark_as_pending_sync()
                # before the sales_service commit.  If we pull them while still
                # pending the client stores them as pending and immediately tries
                # to push them back, causing a NOT NULL constraint on discount_amount
                # and an infinite sync-error loop.
                Sale.sync_status == "synced",
            )
            result.sales = [SaleResponse.model_validate(r) for r in rows]
            total += len(rows)

        if "purchase_orders" in tables:
            rows = await SyncService._pull_table(
                db, PurchaseOrder, since,
                PurchaseOrder.branch_id == branch_id,
                PurchaseOrder.sync_status == "synced",
            )
            result.purchase_orders = [PurchaseOrderResponse.model_validate(r) for r in rows]
            total += len(rows)

        result.total_records = total
        logger.info(
            "Pull completed: branch=%s since=%s records=%d",
            branch_id, since, total,
        )
        return result

    @staticmethod
    async def _pull_table(
        db: AsyncSession,
        model: Any,
        since: Optional[datetime],
        *filters,
    ) -> List[Any]:
        """
        Fetch all rows matching ``filters``, optionally restricted to those
        with ``updated_at > since``.

        Soft-deleted rows (``sync_status='deleted'``) are intentionally
        included so clients know to remove them locally.
        """
        conditions = list(filters)
        if since is not None:
            conditions.append(model.updated_at > since)

        stmt   = select(model).where(and_(*conditions))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # =========================================================================
    # PUSH
    # =========================================================================

    @staticmethod
    async def push(
        db: AsyncSession,
        request: PushRequest,
        organization_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> PushResponse:
        """
        Accept a batch of pending records from the branch.

        Each record is processed inside its own ``begin_nested()`` savepoint.
        A conflict or DB error on record N does not roll back the records
        already accepted for records 1..N-1.  A single ``db.commit()`` is
        issued after the entire batch completes.
        """
        now       = datetime.now(timezone.utc)
        accepted: List[PushResult]   = []
        conflicts: List[PushConflict] = []
        failed: List[PushResult]     = []

        for record in request.records:
            try:
                async with db.begin_nested():  # savepoint per record
                    push_result, conflict = await SyncService._handle_record(
                        db, record, organization_id, request.branch_id, pushed_by
                    )

                if conflict:
                    conflicts.append(conflict)
                elif push_result.success:
                    accepted.append(push_result)
                else:
                    failed.append(push_result)

            except Exception as exc:
                logger.error(
                    "Push failed for %s/%s: %s",
                    record.table_name, record.local_id, exc,
                    exc_info=True,
                )
                failed.append(PushResult(
                    local_id=record.local_id,
                    table_name=record.table_name,
                    success=False,
                    error=str(exc),
                ))

        # Single commit for the entire batch
        await db.commit()

        logger.info(
            "Push completed: branch=%s received=%d accepted=%d conflicts=%d failed=%d",
            request.branch_id, len(request.records),
            len(accepted), len(conflicts), len(failed),
        )

        return PushResponse(
            accepted=accepted,
            conflicts=conflicts,
            failed=failed,
            total_received=len(request.records),
            total_accepted=len(accepted),
            total_conflicts=len(conflicts),
            total_failed=len(failed),
            sync_timestamp=now,
            next_pull_timestamp=now,
        )

    # =========================================================================
    # Record router
    # =========================================================================

    @staticmethod
    async def _handle_record(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Route a single record to the correct push handler."""
        handler = {
            "sales":             SyncService._push_sale,
            "drug_batches":      SyncService._push_batch,
            "stock_adjustments": SyncService._push_adjustment,
            "branch_inventory":  SyncService._push_inventory,
            "purchase_orders":   SyncService._push_purchase_order,
            "customers":         SyncService._push_customer,
        }.get(record.table_name)

        if not handler:
            return PushResult(
                local_id=record.local_id,
                table_name=record.table_name,
                success=False,
                error=f"No handler for table '{record.table_name}'.",
            ), None

        return await handler(db, record, organization_id, branch_id, pushed_by)

    # =========================================================================
    # Per-table push handlers
    # =========================================================================

    @staticmethod
    async def _push_sale(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """
        Sales created offline are always new inserts.

        Idempotency: if the sale already exists (e.g. network retry), return
        success without re-inserting.  The org scope is included in both the
        id and sale_number checks so a sale-number collision from a different
        org is not mistaken for an idempotent re-push.

        Discount mapping: the schema serialises the DB column ``discount_amount``
        as ``total_discount_amount`` (via validation_alias).  The client
        therefore pushes ``total_discount_amount``; we remap it back to the DB
        column name before constructing the Sale model.
        """
        existing = (await db.execute(
            select(Sale).where(
                Sale.organization_id == organization_id,
                or_(
                    Sale.id          == record.local_id,
                    Sale.sale_number == record.data.get("sale_number"),
                ),
            )
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                server_id=str(existing.id),
                success=True,
            ), None

        safe_data = _whitelist(record.data, _SALE_WRITABLE)
        safe_data["organization_id"] = str(organization_id)
        safe_data["branch_id"]       = str(branch_id)

        # Remap schema alias → DB column.
        # The client sends total_discount_amount (the serialised alias of the
        # DB column discount_amount).  Use explicit None checks so that a
        # legitimate zero discount (0 / Decimal("0")) is never skipped.
        if safe_data.get("discount_amount") is None:
            discount = safe_data.get("total_discount_amount")
            if discount is None:
                discount = safe_data.get("contract_discount_amount")
            # Assign outside both inner checks so non-None total_discount_amount
            # is correctly used even when contract_discount_amount is also present.
            safe_data["discount_amount"] = discount if discount is not None else 0

        # Drop schema-only fields — not DB columns; Sale(**safe_data) will reject them.
        safe_data.pop("total_discount_amount", None)
        safe_data.pop("contract_discount_amount", None)
        safe_data.pop("additional_discount_amount", None)

        sale = Sale(**safe_data)
        sale.sync_status = "synced"
        db.add(sale)
        await db.flush()

        return PushResult(
            local_id=record.local_id,
            table_name="sales",
            server_id=str(sale.id),
            success=True,
        ), None

    @staticmethod
    async def _push_batch(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Create or update a DrugBatch with server-wins conflict resolution."""
        existing = (await db.execute(
            select(DrugBatch)
            .where(DrugBatch.id == record.local_id)
            .with_for_update()
        )).scalar_one_or_none()

        if existing:
            conflict = SyncService._check_conflict(existing, record, "drug_batches")
            if conflict:
                return PushResult(
                    local_id=record.local_id, table_name="drug_batches", success=False
                ), conflict
            safe = _whitelist(record.data, _BATCH_WRITABLE)
            for k, v in safe.items():
                setattr(existing, k, v)
            existing.sync_status  = "synced"
            existing.sync_version += 1
        else:
            safe = _whitelist(record.data, _BATCH_WRITABLE)
            safe["branch_id"] = str(branch_id)
            existing = DrugBatch(**safe)
            existing.sync_status = "synced"
            db.add(existing)

        await db.flush()
        return PushResult(
            local_id=record.local_id,
            table_name="drug_batches",
            server_id=str(existing.id),
            success=True,
        ), None

    @staticmethod
    async def _push_adjustment(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """
        StockAdjustments are immutable creates.

        Idempotency: re-push of the same adjustment returns success without
        re-applying the quantity change.

        Conflict: if applying the quantity_change would result in negative
        inventory, return a conflict instead of clamping silently.  Clamping
        hides the discrepancy and corrupts the audit trail.
        """
        existing = (await db.execute(
            select(StockAdjustment).where(StockAdjustment.id == record.local_id)
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                server_id=str(existing.id),
                success=True,
            ), None

        # Persist the adjustment record
        safe = _whitelist(record.data, _ADJUSTMENT_WRITABLE)
        safe["branch_id"]   = str(branch_id)
        safe["adjusted_by"] = str(pushed_by)
        adj = StockAdjustment(**safe)
        db.add(adj)
        await db.flush()

        # Apply quantity change to BranchInventory under a row lock
        drug_id         = record.data.get("drug_id")
        quantity_change = int(record.data.get("quantity_change", 0))

        if drug_id and quantity_change != 0:
            inv = (await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == branch_id,
                    BranchInventory.drug_id   == drug_id,
                )
                .with_for_update()
            )).scalar_one_or_none()

            if inv:
                new_qty = inv.quantity + quantity_change
                if new_qty < 0:
                    # Do not clamp silently — flag as a conflict so the
                    # client knows the device's view of stock was incorrect.
                    return PushResult(
                        local_id=record.local_id,
                        table_name="stock_adjustments",
                        success=False,
                    ), PushConflict(
                        local_id=record.local_id,
                        table_name="stock_adjustments",
                        local_version=record.sync_version,
                        server_version=getattr(inv, "sync_version", 1),
                        server_record={"quantity": inv.quantity},
                        resolution="server_wins",
                    )
                inv.quantity      = new_qty
                inv.sync_version += 1
                inv.mark_as_pending_sync()
            elif quantity_change > 0:
                # No inventory row yet — create one for positive adjustments
                inv = BranchInventory(
                    branch_id=branch_id,
                    drug_id=drug_id,
                    quantity=quantity_change,
                    reserved_quantity=0,
                )
                inv.sync_status = "synced"
                db.add(inv)

            await db.flush()

        return PushResult(
            local_id=record.local_id,
            table_name="stock_adjustments",
            server_id=str(adj.id),
            success=True,
        ), None

    @staticmethod
    async def _push_inventory(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """
        Inventory snapshot update: server_wins on conflict.

        Uses a row-level lock to prevent lost-update races when two devices
        push inventory updates simultaneously.
        """
        drug_id  = record.data.get("drug_id")
        existing = (await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id   == drug_id,
            )
            .with_for_update()
        )).scalar_one_or_none()

        if existing:
            conflict = SyncService._check_conflict(existing, record, "branch_inventory")
            if conflict:
                return PushResult(
                    local_id=record.local_id, table_name="branch_inventory", success=False
                ), conflict
            safe = _whitelist(record.data, _INVENTORY_WRITABLE)
            for k, v in safe.items():
                setattr(existing, k, v)
            existing.sync_status  = "synced"
            existing.sync_version += 1
        else:
            safe = _whitelist(record.data, _INVENTORY_WRITABLE)
            safe["branch_id"] = str(branch_id)
            existing = BranchInventory(**safe)
            existing.sync_status = "synced"
            db.add(existing)

        await db.flush()
        return PushResult(
            local_id=record.local_id,
            table_name="branch_inventory",
            server_id=str(existing.id),
            success=True,
        ), None

    @staticmethod
    async def _push_purchase_order(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Create or update a PurchaseOrder."""
        existing = (await db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.id              == record.local_id,
                PurchaseOrder.organization_id == organization_id,
            )
        )).scalar_one_or_none()

        if existing:
            conflict = SyncService._check_conflict(existing, record, "purchase_orders")
            if conflict:
                return PushResult(
                    local_id=record.local_id, table_name="purchase_orders", success=False
                ), conflict
            safe = _whitelist(record.data, _PO_WRITABLE)
            for k, v in safe.items():
                setattr(existing, k, v)
            existing.sync_status = "synced"
        else:
            safe = _whitelist(record.data, _PO_WRITABLE)
            safe["organization_id"] = str(organization_id)
            safe["branch_id"]       = str(branch_id)
            safe["ordered_by"]      = str(pushed_by)
            existing = PurchaseOrder(**safe)
            existing.sync_status = "synced"
            db.add(existing)

        await db.flush()
        return PushResult(
            local_id=record.local_id,
            table_name="purchase_orders",
            server_id=str(existing.id),
            success=True,
        ), None

    @staticmethod
    async def _push_customer(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """
        Push a customer created offline.

        Idempotency: if the same local_id already exists, return success.
        Deduplication: if another customer in the org shares the same phone
        or email (non-empty, stripped), return a manual_required conflict.
        """
        # Idempotency check
        existing = (await db.execute(
            select(Customer).where(Customer.id == record.local_id)
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="customers",
                server_id=str(existing.id),
                success=True,
            ), None

        # Deduplicate by phone / email — strip and check for non-empty values
        phone = (record.data.get("phone") or "").strip()
        email = (record.data.get("email") or "").strip()

        dupe_conditions = []
        if phone:
            dupe_conditions.append(Customer.phone == phone)
        if email:
            dupe_conditions.append(Customer.email == email)

        if dupe_conditions:
            dupe = (await db.execute(
                select(Customer).where(
                    Customer.organization_id == organization_id,
                    or_(*dupe_conditions),
                )
            )).scalar_one_or_none()

            if dupe:
                return PushResult(
                    local_id=record.local_id, table_name="customers", success=False
                ), PushConflict(
                    local_id=record.local_id,
                    table_name="customers",
                    local_version=record.sync_version,
                    server_version=dupe.sync_version,
                    server_record=CustomerResponse.model_validate(dupe).model_dump(),
                    resolution="manual_required",
                )

        safe = _whitelist(record.data, _CUSTOMER_WRITABLE)
        safe["organization_id"] = str(organization_id)
        customer = Customer(**safe)
        customer.sync_status = "synced"
        db.add(customer)
        await db.flush()

        return PushResult(
            local_id=record.local_id,
            table_name="customers",
            server_id=str(customer.id),
            success=True,
        ), None

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _check_conflict(
        server_record: Any,
        client_record: PushRecord,
        table_name: str,
    ) -> Optional[PushConflict]:
        """
        Return a ``PushConflict`` if the server record has a higher
        ``sync_version`` than the client sent, indicating the client is
        operating on stale data.
        """
        server_version = getattr(server_record, "sync_version", 1)
        if server_version <= client_record.sync_version:
            return None

        resolution = CONFLICT_RESOLUTION.get(table_name, "server_wins")

        _schema_map = {
            "branch_inventory": BranchInventoryResponse,
            "drug_batches":     DrugBatchResponse,
            "purchase_orders":  PurchaseOrderResponse,
            "sales":            SaleResponse,
        }
        schema = _schema_map.get(table_name)
        try:
            server_data = (
                schema.model_validate(server_record).model_dump() if schema else {}
            )
        except Exception:
            logger.warning(
                "Could not serialise server record for conflict response "
                "(table=%s, id=%s)",
                table_name,
                getattr(server_record, "id", "?"),
                exc_info=True,
            )
            server_data = {}

        return PushConflict(
            local_id=client_record.local_id,
            table_name=table_name,
            local_version=client_record.sync_version,
            server_version=server_version,
            server_record=server_data,
            resolution=resolution,
        )
