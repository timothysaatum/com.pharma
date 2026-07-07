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
Branch-owned records created on the server are authoritative for the pulling
client, even if their server-side ``sync_status`` is still ``'pending'`` from a
legacy write path.  Pull responses normalize those rows to ``'synced'`` so the
client stores them without pushing them straight back to the server.

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
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.exc import DBAPIError, OperationalError as SA_OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from dateutil.parser import isoparse
from pydantic import ValidationError

from app.db.session import AsyncSessionLocal
from app.models.customer.customer_model import Customer
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.pricing.pricing_model import InsuranceProvider, PriceContract
from app.models.precriptions.prescription_model import Prescription
from app.models.sales.sales_model import (
    PurchaseOrder,
    PurchaseOrderItem,
    Sale,
    SaleItem,
    SaleItemBatchAllocation,
    Supplier,
)
from app.models.system_md.sys_models import SyncOperationReceipt
from app.models.user.user_model import User
from app.schemas.customer_schemas import CustomerResponse
from app.schemas.drugs_schemas import DrugCategoryResponse, DrugResponse
from app.schemas.inventory_schemas import BranchInventoryResponse, DrugBatchResponse
from app.schemas.price_contract_schemas import PriceContractResponse
from app.schemas.purchase_order_schemas import PurchaseOrderResponse
from app.schemas.sales_schemas import RefundSaleRequest, SaleResponse
from app.schemas.sync_schemas import (
    PullRequest,
    PullResponse,
    PrescriptionSyncResponse,
    PushConflict,
    PushRecord,
    PushRequest,
    PushResponse,
    PushResult,
)
from app.services.inventory.inventory_service import InventoryService
from app.services.sales.pricing.pricing_calculator import d as _d, r2 as _r2
from app.services.sales.utils.sale_helpers import (
    DEFAULT_LOYALTY_THRESHOLDS,
    resolve_loyalty_tier,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server-side validation limits for push data
# These prevent offline client data corruption / injection at sync time.
# ---------------------------------------------------------------------------
MAX_INVENTORY_QUANTITY: int = 1_000_000  # Sanity cap for any inventory qty
MAX_ADJUSTMENT_CHANGE: int = 100_000     # Max qty change per adjustment record
MAX_BATCH_QUANTITY: int = 1_000_000      # Max batch quantity

# ---------------------------------------------------------------------------
# Sync table definitions
# ---------------------------------------------------------------------------
SYNC_TABLES: tuple[str, ...] = (
    "drugs",
    "drug_categories",
    "price_contracts",
    "customers",
    "prescriptions",
    "branch_inventory",
    "drug_batches",
    "sales",
    "purchase_orders",
)

# ---------------------------------------------------------------------------
# Conflict resolution rules per table
# ---------------------------------------------------------------------------
CONFLICT_RESOLUTION: Dict[str, str] = {
    "sales":             "server_wins",
    "branch_inventory":  "server_wins",
    "drug_batches":      "server_wins",
    "stock_adjustments": "server_wins",
    "sale_refunds":      "server_wins",
    "purchase_orders":   "server_wins",
    "customers":         "manual_required",
    "prescriptions":     "server_wins",
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
    "preferred_contract_id",
    "is_active",
    "created_at", "updated_at",
})

_PRESCRIPTION_WRITABLE: frozenset[str] = frozenset({
    "id", "branch_id", "prescription_number", "customer_id",
    "prescriber_name", "prescriber_license", "prescriber_phone",
    "prescriber_address", "issue_date", "expiry_date",
    "medications", "diagnosis", "notes", "special_instructions",
    "refills_allowed", "refills_remaining", "status",
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


def _parse_datetime_fields(data: Dict[str, Any]) -> None:
    """In-place parse ISO datetime/date strings for common timestamp/date keys.

    This converts string values like '2026-05-26T13:42:34.616Z' into
    `datetime` objects so SQLAlchemy/asyncpg accept them for DateTime columns.
    Non-parseable values are left untouched.
    """
    if not data:
        return

    # Candidate keys: anything ending with _at or _date, plus some common names
    extra_keys = {"date_of_birth", "manufacturing_date", "expiry_date", "expected_delivery_date", "received_date"}
    keys = [k for k in data.keys() if k.endswith("_at") or k.endswith("_date") or k in extra_keys]
    for k in keys:
        v = data.get(k)
        if v is None or isinstance(v, (int, float)):
            continue
        if isinstance(v, str):
            try:
                parsed = isoparse(v)
                data[k] = parsed
            except Exception:
                # Best-effort: try stdlib isoformat fallback for Z -> +00:00
                try:
                    from datetime import datetime

                    data[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception:
                    # leave value as-is if parsing fails
                    pass


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    """Normalize optional FK payload values before UUID-column comparisons."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return uuid.UUID(stripped)
        except ValueError:
            return None
    return None


async def _validate_and_fix_sale_fks(
    db: AsyncSession,
    safe_data: Dict[str, Any],
    organization_id: uuid.UUID,
    record_id: str,
    branch_id: uuid.UUID,
    pushed_by: uuid.UUID,
) -> List[str]:
    """
    Validate all foreign keys for a sale record and clear/fix invalid ones.
    
    Args:
        db: AsyncSession for DB queries
        safe_data: Sale data dict (will be modified in-place)
        organization_id: Organization ID for FK scope
        record_id: Record ID for logging
        branch_id: Branch ID for context
    
    Returns:
        List of fixed/cleared FK fields for logging
    
    This prevents foreign key constraint violations by:
    1. Validating optional FKs and clearing invalid ones
    2. Validating required FKs and logging errors
    3. Ensuring data consistency before insert
    """
    fixes: List[str] = []
    
    # ============================================================================
    # OPTIONAL FOREIGN KEYS — clear if referenced record missing
    # ============================================================================
    
    # price_contract_id (nullable, ondelete='SET NULL')
    # This is checked in BOTH organization scope AND globally since it might
    # have been created in a different sync session
    price_contract_id = _uuid_or_none(safe_data.get("price_contract_id"))
    safe_data["price_contract_id"] = price_contract_id
    if price_contract_id is not None:
        # First check: is it in this organization?
        result = await db.execute(
            select(PriceContract.id).where(
                PriceContract.id == price_contract_id,
                PriceContract.organization_id == organization_id,
            )
        )
        contract_in_org = result.scalar_one_or_none()
        
        if contract_in_org is None:
            # Second check: does it exist at all (in any org)?
            result2 = await db.execute(
                select(PriceContract.id).where(
                    PriceContract.id == price_contract_id,
                )
            )
            contract_exists = result2.scalar_one_or_none()
            
            if contract_exists is None:
                logger.warning(
                    "Sync: Missing price_contract %s for sale %s; clearing price_contract_id (not found globally)",
                    price_contract_id, record_id,
                )
            else:
                logger.warning(
                    "Sync: price_contract %s for sale %s not in org %s; clearing price_contract_id",
                    price_contract_id, record_id, organization_id,
                )
            
            safe_data["price_contract_id"] = None
            safe_data["contract_name"] = None
            safe_data["contract_discount_percentage"] = None
            fixes.append(f"price_contract_id={price_contract_id}")

    # customer_id (nullable, ondelete='SET NULL')
    customer_id = _uuid_or_none(safe_data.get("customer_id"))
    safe_data["customer_id"] = customer_id
    if customer_id is not None:
        result = await db.execute(
            select(Customer.id).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            logger.warning(
                "Sync: Missing customer %s for sale %s in org %s; clearing customer_id",
                customer_id, record_id, organization_id,
            )
            safe_data["customer_id"] = None
            fixes.append(f"customer_id={customer_id}")

    # prescription_id (nullable, ondelete='SET NULL')
    prescription_id = _uuid_or_none(safe_data.get("prescription_id"))
    safe_data["prescription_id"] = prescription_id
    if prescription_id is not None:
        result = await db.execute(
            select(Prescription.id).where(
                Prescription.id == prescription_id,
                Prescription.organization_id == organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            logger.warning(
                "Sync: Missing prescription %s for sale %s in org %s; clearing prescription_id",
                prescription_id, record_id, organization_id,
            )
            safe_data["prescription_id"] = None
            fixes.append(f"prescription_id={prescription_id}")

    # pharmacist_id (nullable, ondelete='RESTRICT')
    pharmacist_id = _uuid_or_none(safe_data.get("pharmacist_id"))
    safe_data["pharmacist_id"] = pharmacist_id
    if pharmacist_id is not None:
        result = await db.execute(
            select(User.id).where(
                User.id == pharmacist_id,
                User.organization_id == organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            logger.warning(
                "Sync: Missing pharmacist user %s for sale %s in org %s; clearing pharmacist_id",
                pharmacist_id, record_id, organization_id,
            )
            safe_data["pharmacist_id"] = None
            fixes.append(f"pharmacist_id={pharmacist_id}")

    # ============================================================================
    # REQUIRED FOREIGN KEYS — must exist or sync fails
    # ============================================================================
    
    # cashier_id (nullable=False, ondelete='RESTRICT')
    # Offline sales can outlive the original local user cache. If the payload's
    # cashier is missing/stale, attribute the synced sale to the authenticated
    # user performing the push so the sale is not permanently blocked.
    cashier_id = _uuid_or_none(safe_data.get("cashier_id"))
    result = await db.execute(
        select(User.id).where(
            User.id == cashier_id,
            User.organization_id == organization_id,
        )
    )
    if result.scalar_one_or_none() is None:
        fallback_result = await db.execute(
            select(User.id).where(
                User.id == pushed_by,
                User.organization_id == organization_id,
            )
        )
        if fallback_result.scalar_one_or_none() is None:
            logger.error(
                "Sync: Cashier user %s not found in org %s for sale %s, and "
                "syncing user %s is not a valid fallback.",
                cashier_id, organization_id, record_id, pushed_by,
            )
            raise ValueError(
                f"Cashier {cashier_id} not found in organization {organization_id}. "
                "User may have been deleted or sync order is incorrect."
            )

        logger.warning(
            "Sync: Reassigned cashier for offline sale %s from %s to syncing user %s.",
            record_id, cashier_id, pushed_by,
        )
        safe_data["cashier_id"] = str(pushed_by)
        fixes.append(f"cashier_id={cashier_id}->{pushed_by}")
    else:
        safe_data["cashier_id"] = cashier_id
    
    # branch_id (always set by sync service, validates just to be safe)
    if str(safe_data.get("branch_id")) != str(branch_id):
        logger.error(
            "Sync: branch_id mismatch for sale %s: %s != %s",
            record_id, safe_data.get("branch_id"), branch_id,
        )
        raise ValueError(f"Sale {record_id} has mismatched branch_id")
    
    return fixes


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

        Branch-owned records (sales, inventory, batches) are normalized to
        ``sync_status = 'synced'`` in the response so server-created rows do
        not get queued back as local pending changes after pull.
        """
        # If the request session already has an active transaction (e.g. auth deps
        # queried the DB), PostgreSQL rejects SET TRANSACTION ISOLATION LEVEL
        # as not-first-statement.  Open a fresh read session so the sync snapshot
        # can still choose its isolation level before issuing any query.
        if db.in_transaction() and SyncService._supports_repeatable_read(db):
            async with AsyncSessionLocal() as snapshot_db:
                return await SyncService._pull_with_snapshot(
                    snapshot_db,
                    request,
                    organization_id,
                )

        return await SyncService._pull_with_snapshot(db, request, organization_id)

    @staticmethod
    def _supports_repeatable_read(db: AsyncSession) -> bool:
        return db.get_bind().dialect.name != "sqlite"

    @staticmethod
    async def _pull_with_snapshot(
        db: AsyncSession,
        request: PullRequest,
        organization_id: uuid.UUID,
        skip_isolation: bool = False,
    ) -> PullResponse:
        branch_id = request.branch_id
        tables = set(request.tables or SYNC_TABLES)

        # Capture now BEFORE opening the transaction so the client's next
        # last_sync_at is slightly behind the snapshot point, guaranteeing
        # no records fall in the gap.
        now   = datetime.now(timezone.utc)
        since = request.last_sync_at

        # Request a consistent snapshot so all table queries see the same DB state.
        # SQLite's default transaction isolation is already sufficient and it
        # does not support this PostgreSQL statement.
        if not skip_isolation and SyncService._supports_repeatable_read(db):
            try:
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            except (SA_OperationalError, DBAPIError) as exc:
                if (
                    isinstance(exc, DBAPIError)
                    and "SET TRANSACTION ISOLATION LEVEL must be called before any query"
                    not in str(exc)
                ):
                    raise
                logger.warning(
                    "Could not set repeatable-read isolation for sync pull; "
                    "continuing with the current transaction isolation.",
                    exc_info=True,
                )

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

        if "prescriptions" in tables:
            rows = await SyncService._pull_table(
                db, Prescription, since,
                Prescription.branch_id == branch_id,
            )
            result.prescriptions = [
                PrescriptionSyncResponse.model_validate(r).model_copy(
                    update={"sync_status": "synced"}
                )
                for r in rows
            ]
            total += len(rows)

        if "branch_inventory" in tables:
            rows = await SyncService._pull_table(
                db, BranchInventory, since,
                BranchInventory.branch_id == branch_id,
            )
            result.branch_inventory = [
                BranchInventoryResponse.model_validate(r).model_copy(
                    update={"sync_status": "synced"}
                )
                for r in rows
            ]
            total += len(rows)

        if "drug_batches" in tables:
            rows = await SyncService._pull_table(
                db, DrugBatch, since,
                DrugBatch.branch_id == branch_id,
            )
            result.drug_batches = [
                DrugBatchResponse.model_validate(r).model_copy(
                    update={"sync_status": "synced"}
                )
                for r in rows
            ]
            total += len(rows)

        if "sales" in tables:
            rows = await SyncService._pull_table(
                db, Sale, since,
                Sale.branch_id == branch_id,
                options=[selectinload(Sale.items)],
            )
            result.sales = []
            for row in rows:
                sale_data = {
                    k: v
                    for k, v in row.__dict__.items()
                    if not k.startswith("_") and k != "items"
                }
                # Server branch records are authoritative for the pulling
                # client.  Normalizing to "synced" prevents a pulled server row
                # from being queued back to the server as a local pending change.
                sale_data["sync_status"] = "synced"
                sale_data["items_count"] = sum(
                    int(item.quantity or 0) for item in row.items
                ) if row.items else 0
                result.sales.append(SaleResponse.model_validate(sale_data))
            total += len(rows)

        if "purchase_orders" in tables:
            rows = await SyncService._pull_table(
                db, PurchaseOrder, since,
                PurchaseOrder.branch_id == branch_id,
            )
            result.purchase_orders = [
                PurchaseOrderResponse.model_validate(r).model_copy(
                    update={"sync_status": "synced"}
                )
                for r in rows
            ]
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
        options: Optional[list] = None,
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
        if options:
            stmt = stmt.options(*options)
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

        branch_exists = await db.scalar(
            select(Branch.id).where(
                Branch.id == request.branch_id,
                Branch.organization_id == organization_id,
                Branch.is_active == True,
                Branch.is_deleted == False,
            )
        )
        if not branch_exists:
            error = "Sync branch does not belong to this organization or is inactive."
            logger.warning(
                "Rejected push for invalid branch=%s org=%s records=%d",
                request.branch_id,
                organization_id,
                len(request.records),
            )
            return PushResponse(
                accepted=[],
                conflicts=[],
                failed=[
                    PushResult(
                        local_id=record.local_id,
                        table_name=record.table_name,
                        success=False,
                        error=error,
                    )
                    for record in request.records
                ],
                total_received=len(request.records),
                total_accepted=0,
                total_conflicts=0,
                total_failed=len(request.records),
                sync_timestamp=now,
                next_pull_timestamp=now,
            )

        table_priority = {
            "price_contracts": 0,
            "customers": 1,
            "prescriptions": 2,
            "drugs": 3,
            "drug_categories": 4,
            "branch_inventory": 5,
            "drug_batches": 6,
            "stock_adjustments": 7,
            "purchase_orders": 8,
            "sales": 9,
            "sale_refunds": 10,
        }

        sorted_records = sorted(
            request.records,
            key=lambda record: table_priority.get(record.table_name, 99)
        )

        for record in sorted_records:
            try:
                if record.operation_id:
                    receipt = await db.get(
                        SyncOperationReceipt,
                        record.operation_id,
                    )
                    if receipt:
                        if (
                            receipt.organization_id != organization_id
                            or receipt.branch_id != request.branch_id
                            or receipt.table_name != record.table_name
                            or receipt.record_id != record.local_id
                        ):
                            failed.append(PushResult(
                                local_id=record.local_id,
                                table_name=record.table_name,
                                success=False,
                                error=(
                                    "Operation ID is already bound to a different "
                                    "sync mutation."
                                ),
                            ))
                            continue
                        SyncService._append_receipt_result(
                            receipt,
                            accepted,
                            conflicts,
                            failed,
                        )
                        continue

                async with db.begin_nested():  # savepoint per record
                    receipt = None
                    if record.operation_id:
                        # Flush the receipt before applying the mutation. A
                        # concurrent request with the same operation ID then
                        # fails at this savepoint before it can mutate data.
                        receipt = SyncOperationReceipt(
                            operation_id=record.operation_id,
                            organization_id=organization_id,
                            branch_id=request.branch_id,
                            table_name=record.table_name,
                            record_id=record.local_id,
                            result_kind="failed",
                            response_data={},
                        )
                        db.add(receipt)
                        await db.flush()

                    push_result, conflict = await SyncService._handle_record(
                        db, record, organization_id, request.branch_id, pushed_by
                    )

                    if receipt:
                        if conflict:
                            receipt.result_kind = "conflict"
                            receipt.response_data = conflict.model_dump(mode="json")
                        elif push_result.success:
                            receipt.result_kind = "accepted"
                            receipt.response_data = push_result.model_dump(mode="json")
                        else:
                            receipt.result_kind = "failed"
                            receipt.response_data = push_result.model_dump(mode="json")

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
        if record.operation == "delete":
            return PushResult(
                local_id=record.local_id,
                table_name=record.table_name,
                success=False,
                error=(
                    "Offline delete is not supported for this resource. "
                    "Reconnect and use the resource-specific delete endpoint."
                ),
            ), None

        handler = {
            "sales":             SyncService._push_sale,
            "sale_refunds":      SyncService._push_refund,
            "drug_batches":      SyncService._push_batch,
            "stock_adjustments": SyncService._push_adjustment,
            "branch_inventory":  SyncService._push_inventory,
            "purchase_orders":   SyncService._push_purchase_order,
            "customers":         SyncService._push_customer,
            "prescriptions":      SyncService._push_prescription,
        }.get(record.table_name)

        if not handler:
            return PushResult(
                local_id=record.local_id,
                table_name=record.table_name,
                success=False,
                error=f"No handler for table '{record.table_name}'.",
            ), None

        return await handler(db, record, organization_id, branch_id, pushed_by)

    @staticmethod
    def _append_receipt_result(
        receipt: SyncOperationReceipt,
        accepted: List[PushResult],
        conflicts: List[PushConflict],
        failed: List[PushResult],
    ) -> None:
        """Replay the exact durable result for an idempotent operation retry."""
        if receipt.result_kind == "accepted":
            accepted.append(PushResult.model_validate(receipt.response_data))
        elif receipt.result_kind == "conflict":
            conflicts.append(PushConflict.model_validate(receipt.response_data))
        else:
            failed.append(PushResult.model_validate(receipt.response_data))

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
        
        Foreign Key Validation:
        - Comprehensive FK validation prevents constraint violations
        - Optional FKs (customer, contract, pharmacist) are cleared if missing
        - Required FKs (cashier, branch) must exist or sync fails with error
        - All fixes/validations are logged for audit trail
        """
        if record.operation != "create":
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error="Sales are immutable after creation; use cancel/refund endpoints.",
            ), None

        existing = (await db.execute(
            select(Sale).where(
                Sale.organization_id == organization_id,
                Sale.branch_id == branch_id,
                Sale.id == record.local_id,
            )
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                server_id=str(existing.id),
                success=True,
            ), None

        other_branch_sale = await db.scalar(
            select(Sale.id).where(
                Sale.organization_id == organization_id,
                Sale.id == record.local_id,
                Sale.branch_id != branch_id,
            )
        )
        if other_branch_sale:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error="Sale does not belong to this sync branch.",
            ), None

        sale_number_collision = (await db.execute(
            select(Sale.id).where(
                Sale.branch_id == branch_id,
                Sale.sale_number == record.data.get("sale_number"),
            )
        )).scalar_one_or_none()
        if sale_number_collision:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error=(
                    "Sale number already belongs to another transaction. "
                    "The local sale was not imported."
                ),
            ), None

        items_data = record.data.get("items")
        if not isinstance(items_data, list) or not items_data:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error="A completed sale must contain at least one line item.",
            ), None

        normalized_items: list[dict[str, Any]] = []
        item_drug_ids: set[uuid.UUID] = set()
        item_subtotal = Decimal("0")
        item_discount = Decimal("0")
        item_tax = Decimal("0")
        item_total = Decimal("0")

        for item in items_data:
            if not isinstance(item, dict):
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error="Every sale item must be an object.",
                ), None
            try:
                drug_id = uuid.UUID(str(item.get("drug_id")))
                quantity = int(item.get("quantity", 0))
                unit_price = Decimal(str(item.get("unit_price", 0)))
                subtotal = Decimal(str(item.get("subtotal", 0)))
                discount_amount = Decimal(str(item.get("discount_amount") or 0))
                tax_amount = Decimal(str(item.get("tax_amount") or 0))
                total_price = Decimal(str(item.get("total_price", 0)))
            except (ArithmeticError, TypeError, ValueError):
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error="Sale item values are invalid.",
                ), None

            monetary_values = (
                unit_price,
                subtotal,
                discount_amount,
                tax_amount,
                total_price,
            )
            if not all(value.is_finite() for value in monetary_values):
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error="Sale item monetary values must be finite.",
                ), None
            if quantity <= 0 or unit_price < 0:
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error="Sale item quantity must be positive and price non-negative.",
                ), None
            expected_subtotal = unit_price * quantity
            expected_total = subtotal - discount_amount + tax_amount
            if (
                abs(subtotal - expected_subtotal) > Decimal("0.01")
                or discount_amount < 0
                or discount_amount > subtotal
                or tax_amount < 0
                or abs(total_price - expected_total) > Decimal("0.01")
            ):
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error="Sale line financial totals are inconsistent.",
                ), None

            normalized = dict(item)
            normalized["drug_id"] = drug_id
            normalized["quantity"] = quantity
            normalized["unit_price"] = unit_price
            normalized["subtotal"] = subtotal
            normalized["discount_amount"] = discount_amount
            normalized["tax_amount"] = tax_amount
            normalized["total_price"] = total_price
            normalized_items.append(normalized)
            item_drug_ids.add(drug_id)
            item_subtotal += subtotal
            item_discount += discount_amount
            item_tax += tax_amount
            item_total += total_price

        existing_drug_ids = set((await db.execute(
            select(Drug.id).where(
                Drug.id.in_(item_drug_ids),
                Drug.organization_id == organization_id,
                Drug.is_deleted == False,
                Drug.is_active == True,
            )
        )).scalars().all())
        if existing_drug_ids != item_drug_ids:
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error=f"Drugs not found or inactive: {item_drug_ids - existing_drug_ids}",
            ), None

        prescription_required_drug_ids = set((await db.execute(
            select(Drug.id).where(
                Drug.id.in_(item_drug_ids),
                Drug.organization_id == organization_id,
                Drug.requires_prescription == True,
            )
        )).scalars().all())

        header_values = {
            "subtotal": item_subtotal,
            "discount_amount": item_discount,
            "tax_amount": item_tax,
            "total_amount": item_total,
        }
        for field, expected in header_values.items():
            raw_value = record.data.get(field)
            if field == "discount_amount" and raw_value is None:
                raw_value = record.data.get("total_discount_amount", 0)
            try:
                actual = Decimal(str(raw_value or 0))
            except (ArithmeticError, TypeError, ValueError):
                actual = Decimal("-1")
            if (
                not actual.is_finite()
                or abs(actual - expected) > Decimal("0.01")
            ):
                return PushResult(
                    local_id=record.local_id,
                    table_name="sales",
                    success=False,
                    error=f"Sale {field} does not match its line items.",
                ), None

        safe_data = _whitelist(record.data, _SALE_WRITABLE)
        _parse_datetime_fields(safe_data)
        safe_data["id"] = record.local_id
        safe_data["organization_id"] = str(organization_id)
        safe_data["branch_id"]       = str(branch_id)
        safe_data["updated_at"] = datetime.now(timezone.utc)

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

        # ======================================================================
        # COMPREHENSIVE FOREIGN KEY VALIDATION
        # ======================================================================
        fk_fixes: List[str] = []
        try:
            fixes = await _validate_and_fix_sale_fks(
                db, safe_data, organization_id, record.local_id, branch_id, pushed_by
            )
            fk_fixes.extend(fixes)
            if fk_fixes:
                logger.info(
                    "Sync: Sale %s had FK fixes: %s",
                    record.local_id, ", ".join(fk_fixes),
                )
        except ValueError as exc:
            logger.error(
                "Sync: FK validation failed for sale %s: %s",
                record.local_id, exc,
            )
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error=f"Foreign key validation failed: {exc}",
                fk_fixes=fk_fixes if fk_fixes else None,
            ), None

        if prescription_required_drug_ids and not safe_data.get("prescription_id"):
            return PushResult(
                local_id=record.local_id,
                table_name="sales",
                success=False,
                error=(
                    "Sale contains prescription-required drugs but no valid "
                    "prescription was synced."
                ),
                fk_fixes=fk_fixes if fk_fixes else None,
            ), None

        try:
            inventory_plan = None
            if record.data.get("sync_protocol_version") == 2:
                inventory_plan, inventory_error = (
                    await SyncService._prepare_offline_sale_inventory(
                        db=db,
                        branch_id=branch_id,
                        items=normalized_items,
                    )
                )
                if inventory_error:
                    return PushResult(
                        local_id=record.local_id,
                        table_name="sales",
                        success=False,
                        error=inventory_error,
                    ), None

            sale = Sale(**safe_data)
            sale.sync_status = "synced"
            db.add(sale)
            await db.flush()

            # Sync SaleItems if present in record data
            created_sale_items: list[SaleItem] = []
            if normalized_items:
                for item_dict in normalized_items:
                    # Whitelist SaleItem fields
                    item_safe = {
                        k: v for k, v in item_dict.items()
                        if k in {
                            "id", "drug_id", "drug_name", "drug_sku", "batch_id",
                            "quantity", "unit_price", "subtotal", "discount_percentage",
                            "discount_amount", "tax_rate", "tax_amount", "total_price",
                            "requires_prescription", "prescription_verified",
                            "created_at", "updated_at"
                        }
                    }
                    _parse_datetime_fields(item_safe)
                    item_safe["sale_id"] = sale.id

                    sale_item = SaleItem(**item_safe)
                    db.add(sale_item)
                    created_sale_items.append(sale_item)
                await db.flush()

            if inventory_plan is not None:
                await SyncService._apply_offline_sale_inventory(
                    db=db,
                    sale=sale,
                    sale_items=created_sale_items,
                    organization_id=organization_id,
                    branch_id=branch_id,
                    pushed_by=pushed_by,
                    inventory_plan=inventory_plan,
                )

            # ------------------------------------------------------------------
            # 18. Prescription refill decrement (critical for offline sales)
            #     Uses sync_version to prevent double-decrement when prescription
            #     refills were already decremented offline.
            # ------------------------------------------------------------------
            if safe_data.get("prescription_id"):
                try:
                    rx_res = await db.execute(
                        select(Prescription).where(
                            Prescription.id == safe_data["prescription_id"],
                            Prescription.organization_id == organization_id,
                        )
                    )
                    prescription = rx_res.scalar_one_or_none()
                    if not prescription:
                        raise ValueError("Prescription no longer exists.")
                    if str(prescription.branch_id) != str(branch_id):
                        raise ValueError(
                            "Prescription belongs to a different branch."
                        )
                    if prescription.status != "active":
                        raise ValueError(
                            f"Prescription is {prescription.status} and cannot be filled."
                        )
                    if prescription.expiry_date < date.today():
                        raise ValueError("Prescription has expired.")
                    if prescription.refills_remaining <= 0:
                        raise ValueError("Prescription has no refills remaining.")

                    # The sale ID and operation receipt provide idempotency, so
                    # the server applies this side effect exactly once.
                    prescription.refills_remaining -= 1
                    prescription.sync_version += 1
                    prescription.last_refill_date = date.today()
                    prescription.status = (
                        "filled" if prescription.refills_remaining == 0 else "active"
                    )
                    prescription.updated_at = datetime.now(timezone.utc)
                    prescription.mark_as_synced()
                    logger.info(
                        "Sync: decremented refills for prescription %s during sale push",
                        safe_data["prescription_id"],
                    )
                except Exception as rx_exc:
                    logger.error(
                        "Sync: Failed to decrement prescription refills during sale push: %s",
                        rx_exc,
                        exc_info=True,
                    )
                    raise RuntimeError(
                        "Prescription refill update failed; sale was rolled back."
                    ) from rx_exc

        except Exception as exc:
            # Let the per-record savepoint unwind every sale side effect,
            # including inventory, allocations, and ledger rows. Attempting an
            # in-place "recovery" here can commit a partial financial record.
            logger.error(
                "Sync: failed to persist sale %s safely: %s",
                record.local_id,
                exc,
                exc_info=True,
            )
            raise RuntimeError("Sale could not be persisted safely.") from exc

        return PushResult(
            local_id=record.local_id,
            table_name="sales",
            server_id=str(sale.id),
            success=True,
            fk_fixes=fk_fixes if fk_fixes else None,
        ), None

    @staticmethod
    async def _push_refund(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Apply an offline refund command exactly once."""
        if record.operation != "create":
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error="Refund sync records are immutable create commands.",
            ), None

        try:
            refund_id = uuid.UUID(str(record.local_id))
        except (TypeError, ValueError):
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error="Refund local_id must be a valid UUID.",
            ), None

        previous_receipt = (await db.execute(
            select(SyncOperationReceipt).where(
                SyncOperationReceipt.organization_id == organization_id,
                SyncOperationReceipt.branch_id == branch_id,
                SyncOperationReceipt.table_name == "sale_refunds",
                SyncOperationReceipt.record_id == record.local_id,
                SyncOperationReceipt.result_kind == "accepted",
            )
        )).scalar_one_or_none()
        if previous_receipt:
            return PushResult.model_validate(previous_receipt.response_data), None

        try:
            sale_id = uuid.UUID(str(record.data.get("sale_id")))
            refund_data = RefundSaleRequest.model_validate(record.data)
        except (TypeError, ValueError, ValidationError) as exc:
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=f"Refund payload is invalid: {exc}",
            ), None

        user = await db.scalar(
            select(User)
            .where(
                User.id == pushed_by,
                User.organization_id == organization_id,
                User.is_active == True,
            )
            .options(selectinload(User.roles))
        )
        if not user or not user.has_permission("process_refunds"):
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error="Syncing user cannot process refunds.",
            ), None

        assigned = [str(b) for b in (user.assigned_branches or [])]
        if str(branch_id) not in assigned and not user.is_super_admin:
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error="Syncing user does not have access to this branch.",
            ), None

        approver_id = refund_data.manager_approval_user_id
        if approver_id == user.id:
            approver = user
        else:
            approver = await db.scalar(
                select(User)
                .where(
                    User.id == approver_id,
                    User.organization_id == organization_id,
                    User.is_active == True,
                )
                .options(selectinload(User.roles))
            )
        if not approver or not approver.has_permission("process_refunds"):
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error="Approving manager cannot process refunds.",
            ), None

        sale = (await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(
                Sale.id == sale_id,
                Sale.organization_id == organization_id,
                Sale.branch_id == branch_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if not sale:
            other_branch_sale = await db.scalar(
                select(Sale.id).where(
                    Sale.id == sale_id,
                    Sale.organization_id == organization_id,
                    Sale.branch_id != branch_id,
                )
            )
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=(
                    "Sale does not belong to this sync branch."
                    if other_branch_sale
                    else "Sale to refund was not found."
                ),
            ), None

        if sale.status not in ("completed", "partially_refunded"):
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=(
                    f"Cannot refund a sale with status '{sale.status}'. "
                    "Only completed or partially refunded sales may be refunded."
                ),
            ), None

        refund_amount = _r2(_d(refund_data.refund_amount))
        existing_refund_amount = _r2(_d(sale.refund_amount or 0))
        sale_total = _r2(_d(sale.total_amount))
        new_refund_total = _r2(existing_refund_amount + refund_amount)
        if new_refund_total > sale_total:
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=(
                    f"Refund total ({new_refund_total}) cannot exceed sale "
                    f"total ({sale_total}). Already refunded: {existing_refund_amount}."
                ),
            ), None

        locked_items = list((await db.execute(
            select(SaleItem)
            .where(SaleItem.sale_id == sale.id)
            .with_for_update()
        )).scalars().all())
        sale_item_map: Dict[uuid.UUID, SaleItem] = {
            item.id: item for item in locked_items
        }
        refund_item_ids = {ri.sale_item_id for ri in refund_data.items_to_refund}
        invalid = refund_item_ids - set(sale_item_map)
        if invalid:
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=f"Refund items not found in the original sale: {invalid}",
            ), None

        for refund_item in refund_data.items_to_refund:
            sale_item = sale_item_map[refund_item.sale_item_id]
            already_refunded = int(sale_item.refunded_quantity or 0)
            remaining_quantity = int(sale_item.quantity) - already_refunded
            if refund_item.quantity > remaining_quantity:
                return PushResult(
                    local_id=record.local_id,
                    table_name="sale_refunds",
                    success=False,
                    error=(
                        f"Cannot refund {refund_item.quantity} units of "
                        f"'{sale_item.drug_name}'. Remaining refundable "
                        f"quantity: {remaining_quantity}."
                    ),
                ), None

        expected_refund = _r2(sum(
            (
                _d(sale_item_map[item.sale_item_id].total_price)
                * Decimal(item.quantity)
                / Decimal(sale_item_map[item.sale_item_id].quantity)
            )
            for item in refund_data.items_to_refund
        ))
        if abs(refund_amount - expected_refund) > Decimal("0.005"):
            return PushResult(
                local_id=record.local_id,
                table_name="sale_refunds",
                success=False,
                error=(
                    f"Refund amount ({refund_amount}) must equal the selected "
                    f"item value ({expected_refund})."
                ),
            ), None

        now = datetime.now(timezone.utc)
        is_full_refund = new_refund_total == sale_total
        sale.status = "refunded" if is_full_refund else "partially_refunded"
        sale.payment_status = "refunded" if is_full_refund else "partial"
        sale.refund_amount = new_refund_total
        sale.refunded_at = now
        sale.refunded_by = pushed_by
        sale.refund_reason = refund_data.reason
        sale.refund_reference = (
            str(record.data.get("refund_reference") or f"OFFLINE-{refund_id}")
        )
        sale.notes = f"Refunded offline: {refund_data.reason}\n\n{sale.notes or ''}".strip()
        sale.updated_at = now
        sale.sync_version += 1
        sale.mark_as_synced()

        if sale.prescription_id:
            prescription = await db.scalar(
                select(Prescription)
                .where(
                    Prescription.id == sale.prescription_id,
                    Prescription.organization_id == organization_id,
                    Prescription.branch_id == branch_id,
                )
                .with_for_update()
            )
            if (
                prescription
                and prescription.refills_remaining < prescription.refills_allowed
            ):
                prescription.refills_remaining += 1
                prescription.status = "active"
                prescription.updated_at = now
                prescription.sync_version += 1
                prescription.mark_as_synced()

        inventory_restored = 0
        batches_restored = 0

        for refund_item in refund_data.items_to_refund:
            sale_item = sale_item_map[refund_item.sale_item_id]
            sale_item.refunded_quantity = int(
                sale_item.refunded_quantity or 0
            ) + refund_item.quantity
            sale_item.updated_at = now

            allocations = list((await db.execute(
                select(SaleItemBatchAllocation)
                .where(SaleItemBatchAllocation.sale_item_id == sale_item.id)
                .order_by(SaleItemBatchAllocation.created_at.asc())
                .with_for_update()
            )).scalars().all())

            restore_targets: list[dict[str, Any]] = []
            qty_to_allocate = refund_item.quantity
            for allocation in allocations:
                if qty_to_allocate <= 0:
                    break
                allocation_remaining = (
                    int(allocation.quantity) - int(allocation.refunded_quantity or 0)
                )
                if allocation_remaining <= 0:
                    continue
                consume_qty = min(allocation_remaining, qty_to_allocate)
                allocation.refunded_quantity = int(
                    allocation.refunded_quantity or 0
                ) + consume_qty
                allocation.updated_at = now
                restore_targets.append({
                    "batch_id": allocation.batch_id,
                    "batch_number": allocation.batch_number,
                    "batch_expiry_date": allocation.batch_expiry_date,
                    "quantity": consume_qty,
                })
                qty_to_allocate -= consume_qty

            if allocations and qty_to_allocate > 0:
                raise RuntimeError(
                    "Refund quantity exceeds remaining sale batch allocations."
                )

            if not allocations:
                restore_targets = [{
                    "batch_id": sale_item.batch_id,
                    "batch_number": None,
                    "batch_expiry_date": None,
                    "quantity": refund_item.quantity,
                }]

            if not refund_item.restock:
                continue

            inventory = (await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == branch_id,
                    BranchInventory.drug_id == sale_item.drug_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if not inventory:
                raise RuntimeError(
                    f"Inventory record missing for refunded drug {sale_item.drug_id}."
                )

            previous_qty = int(inventory.quantity)
            running_inventory_qty = previous_qty
            inventory_restored += 1

            qty_to_restore = refund_item.quantity
            for target in restore_targets:
                if qty_to_restore <= 0:
                    break
                restore_qty = min(int(target["quantity"]), qty_to_restore)
                batch = None
                if target["batch_id"]:
                    batch = (await db.execute(
                        select(DrugBatch)
                        .where(
                            DrugBatch.id == target["batch_id"],
                            DrugBatch.branch_id == branch_id,
                        )
                        .with_for_update()
                    )).scalar_one_or_none()

                if batch is None:
                    batch = DrugBatch(
                        id=uuid.uuid4(),
                        branch_id=branch_id,
                        drug_id=sale_item.drug_id,
                        batch_number=(
                            target["batch_number"]
                            or f"RETURN-{sale.sale_number}-{str(sale_item.id)[:8]}"
                        ),
                        quantity=restore_qty,
                        remaining_quantity=restore_qty,
                        expiry_date=(
                            target["batch_expiry_date"]
                            or date.today() + timedelta(days=365 * 10)
                        ),
                        sync_status="synced",
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(batch)
                    await db.flush()

                previous_batch_qty = int(batch.remaining_quantity)
                batch.remaining_quantity = previous_batch_qty + restore_qty
                if batch.remaining_quantity > batch.quantity:
                    batch.quantity = batch.remaining_quantity
                batch.updated_at = now
                batch.sync_version += 1
                batch.mark_as_synced()
                batches_restored += 1

                movement_before = running_inventory_qty
                running_inventory_qty += restore_qty
                await InventoryService._record_inventory_movement(
                    db=db,
                    organization_id=organization_id,
                    branch_id=branch_id,
                    drug_id=sale_item.drug_id,
                    movement_type="refund",
                    quantity_change=restore_qty,
                    quantity_before=movement_before,
                    quantity_after=running_inventory_qty,
                    batch_id=batch.id,
                    batch_quantity_before=previous_batch_qty,
                    batch_quantity_after=batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(batch.cost_price))
                        if batch.cost_price is not None
                        else None
                    ),
                    unit_price=Decimal(str(sale_item.unit_price)),
                    source_type="sale",
                    source_id=sale.id,
                    source_line_id=sale_item.id,
                    reference_number=sale.sale_number,
                    reason=f"Offline refund {refund_id}: {refund_data.reason}",
                    created_by=pushed_by,
                )
                qty_to_restore -= restore_qty

            if qty_to_restore > 0:
                raise RuntimeError(
                    "Refund restock quantity could not be allocated to sale batches."
                )

            inventory.quantity = running_inventory_qty
            inventory.updated_at = now
            inventory.sync_version += 1
            inventory.mark_as_synced()

            db.add(StockAdjustment(
                id=uuid.uuid4(),
                branch_id=branch_id,
                drug_id=sale_item.drug_id,
                adjustment_type="return",
                quantity_change=refund_item.quantity,
                previous_quantity=previous_qty,
                new_quantity=running_inventory_qty,
                reason=f"Offline refund {refund_id}: {refund_data.reason}",
                adjusted_by=pushed_by,
                created_at=now,
                updated_at=now,
            ))

        loyalty_points_deducted = 0
        if sale.customer_id:
            customer = (await db.execute(
                select(Customer)
                .where(
                    Customer.id == sale.customer_id,
                    Customer.organization_id == organization_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if customer:
                organization = await db.get(Organization, organization_id)
                loyalty_enabled = bool(
                    organization
                    and organization.settings.get("enable_loyalty_program", False)
                )
                if loyalty_enabled:
                    loyalty_cfg = organization.settings.get("loyalty", {})
                    points_rate = _d(loyalty_cfg.get("points_per_unit", "1.0"))
                    points_to_deduct = int(refund_amount * points_rate)
                    customer.loyalty_points = max(
                        0,
                        int(customer.loyalty_points or 0) - points_to_deduct,
                    )
                    customer.loyalty_tier = resolve_loyalty_tier(
                        customer.loyalty_points,
                        loyalty_cfg.get("tier_thresholds", DEFAULT_LOYALTY_THRESHOLDS),
                    )
                    loyalty_points_deducted = points_to_deduct
                customer.total_value = _r2(
                    _d(customer.total_value or 0) - refund_amount
                )
                if customer.total_value < 0:
                    customer.total_value = Decimal("0.00")
                customer.updated_at = now
                customer.sync_version += 1
                customer.mark_as_synced()

        await db.flush()
        return PushResult(
            local_id=record.local_id,
            table_name="sale_refunds",
            server_id=str(refund_id),
            success=True,
            fk_fixes=[
                f"inventory_restored={inventory_restored}",
                f"batches_restored={batches_restored}",
                f"loyalty_points_deducted={loyalty_points_deducted}",
            ],
        ), None

    @staticmethod
    async def _prepare_offline_sale_inventory(
        db: AsyncSession,
        branch_id: uuid.UUID,
        items: list[dict[str, Any]],
    ) -> tuple[
        Optional[tuple[dict[uuid.UUID, BranchInventory], dict[uuid.UUID, list[DrugBatch]]]],
        Optional[str],
    ]:
        """Lock and validate all stock needed by a protocol-v2 offline sale."""
        required: dict[uuid.UUID, int] = {}
        for item in items:
            drug_id = item["drug_id"]
            required[drug_id] = required.get(drug_id, 0) + int(item["quantity"])

        inventory_rows = (await db.execute(
            select(BranchInventory)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.drug_id.in_(required),
            )
            .with_for_update()
        )).scalars().all()
        inventories = {row.drug_id: row for row in inventory_rows}

        missing_inventory = set(required) - set(inventories)
        if missing_inventory:
            return None, f"Inventory records not found for drugs: {missing_inventory}"

        for drug_id, quantity in required.items():
            inventory = inventories[drug_id]
            available = inventory.quantity - inventory.reserved_quantity
            if available < quantity:
                return None, (
                    f"Insufficient stock for drug {drug_id}. "
                    f"Available: {available}, requested: {quantity}."
                )

        batch_rows = (await db.execute(
            select(DrugBatch)
            .where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id.in_(required),
                DrugBatch.remaining_quantity > 0,
                DrugBatch.expiry_date > date.today(),
            )
            .order_by(DrugBatch.drug_id, DrugBatch.expiry_date)
            .with_for_update()
        )).scalars().all()
        batches: dict[uuid.UUID, list[DrugBatch]] = {}
        for batch in batch_rows:
            batches.setdefault(batch.drug_id, []).append(batch)

        for drug_id, quantity in required.items():
            available = sum(
                batch.remaining_quantity for batch in batches.get(drug_id, [])
            )
            if available < quantity:
                return None, (
                    f"Insufficient non-expired batch stock for drug {drug_id}. "
                    f"Available: {available}, requested: {quantity}."
                )

        return (inventories, batches), None

    @staticmethod
    async def _apply_offline_sale_inventory(
        db: AsyncSession,
        sale: Sale,
        sale_items: list[SaleItem],
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
        inventory_plan: tuple[
            dict[uuid.UUID, BranchInventory],
            dict[uuid.UUID, list[DrugBatch]],
        ],
    ) -> None:
        """Apply sale, batch allocations, ledger, and stock audit atomically."""
        inventories, batches_by_drug = inventory_plan
        now = datetime.now(timezone.utc)

        for sale_item in sale_items:
            inventory = inventories[sale_item.drug_id]
            previous_quantity = inventory.quantity
            running_quantity = previous_quantity
            remaining = sale_item.quantity
            primary_batch_id: Optional[uuid.UUID] = None

            for batch in batches_by_drug[sale_item.drug_id]:
                if remaining <= 0:
                    break
                take = min(batch.remaining_quantity, remaining)
                if take <= 0:
                    continue

                if primary_batch_id is None:
                    primary_batch_id = batch.id
                batch_before = batch.remaining_quantity
                batch.remaining_quantity -= take
                batch.sync_version += 1
                batch.sync_status = "synced"
                batch.updated_at = now
                remaining -= take

                db.add(SaleItemBatchAllocation(
                    id=uuid.uuid4(),
                    sale_item_id=sale_item.id,
                    branch_id=branch_id,
                    drug_id=sale_item.drug_id,
                    batch_id=batch.id,
                    batch_number=batch.batch_number,
                    batch_expiry_date=batch.expiry_date,
                    quantity=take,
                    refunded_quantity=0,
                    unit_cost_at_sale=batch.cost_price,
                    unit_price_at_sale=sale_item.unit_price,
                    created_at=now,
                    updated_at=now,
                ))

                movement_before = running_quantity
                running_quantity -= take
                await InventoryService._record_inventory_movement(
                    db=db,
                    organization_id=organization_id,
                    branch_id=branch_id,
                    drug_id=sale_item.drug_id,
                    movement_type="sale",
                    quantity_change=-take,
                    quantity_before=movement_before,
                    quantity_after=running_quantity,
                    batch_id=batch.id,
                    batch_quantity_before=batch_before,
                    batch_quantity_after=batch.remaining_quantity,
                    unit_cost=(
                        Decimal(str(batch.cost_price))
                        if batch.cost_price is not None
                        else None
                    ),
                    unit_price=Decimal(str(sale_item.unit_price)),
                    source_type="sale",
                    source_id=sale.id,
                    source_line_id=sale_item.id,
                    reference_number=sale.sale_number,
                    reason=f"Offline sale {sale.sale_number}",
                    created_by=pushed_by,
                )

            # Preflight validation guarantees this cannot remain positive.
            if remaining:
                raise RuntimeError(
                    f"Locked batch stock changed unexpectedly for {sale_item.drug_id}."
                )

            sale_item.batch_id = primary_batch_id
            inventory.quantity = running_quantity
            inventory.sync_version += 1
            inventory.sync_status = "synced"
            inventory.updated_at = now

            db.add(StockAdjustment(
                id=uuid.uuid4(),
                branch_id=branch_id,
                drug_id=sale_item.drug_id,
                adjustment_type="correction",
                quantity_change=-sale_item.quantity,
                previous_quantity=previous_quantity,
                new_quantity=running_quantity,
                reason=f"Offline sale {sale.sale_number}",
                adjusted_by=pushed_by,
                created_at=now,
                updated_at=now,
            ))

        await db.flush()

    @staticmethod
    async def _push_batch(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Create a batch, atomically applying protocol-v2 goods receipts."""
        if record.operation != "create":
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error=(
                    "Offline batch correction is not supported. Reconnect and "
                    "use the batch update endpoint so inventory is recalculated."
                ),
            ), None

        existing = (await db.execute(
            select(DrugBatch)
            .where(
                DrugBatch.id == record.local_id,
                DrugBatch.branch_id == branch_id,
            )
            .with_for_update()
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                server_id=str(existing.id),
                success=True,
            ), None

        other_branch_batch = await db.scalar(
            select(DrugBatch.id).where(
                DrugBatch.id == record.local_id,
                DrugBatch.branch_id != branch_id,
            )
        )
        if other_branch_batch:
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error="Drug batch does not belong to this sync branch.",
            ), None

        safe = _whitelist(record.data, _BATCH_WRITABLE)
        _parse_datetime_fields(safe)
        safe["id"] = record.local_id
        safe["branch_id"] = str(branch_id)
        safe["updated_at"] = datetime.now(timezone.utc)

        drug_id = safe.get("drug_id")
        drug_exists = await db.scalar(
            select(Drug.id).where(
                Drug.id == drug_id,
                Drug.organization_id == organization_id,
                Drug.is_active == True,
                Drug.is_deleted == False,
            )
        )
        if not drug_exists:
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error=f"Drug {drug_id} not found or inactive in organization.",
            ), None

        try:
            quantity = int(safe.get("quantity", 0))
            remaining_quantity = int(safe.get("remaining_quantity", quantity))
        except (TypeError, ValueError):
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error="Batch quantities must be valid integers.",
            ), None
        if (
            quantity <= 0
            or quantity > MAX_BATCH_QUANTITY
            or remaining_quantity < 0
            or remaining_quantity > quantity
        ):
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error="Batch quantities are outside the accepted range.",
            ), None
        safe["quantity"] = quantity
        safe["remaining_quantity"] = remaining_quantity

        duplicate = await db.scalar(
            select(DrugBatch.id).where(
                DrugBatch.branch_id == branch_id,
                DrugBatch.drug_id == drug_id,
                DrugBatch.batch_number == safe.get("batch_number"),
            )
        )
        if duplicate:
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error="A batch with this number already exists for the drug.",
            ), None

        purchase_order_id = safe.get("purchase_order_id")
        if purchase_order_id:
            purchase_order_exists = await db.scalar(
                select(PurchaseOrder.id).where(
                    PurchaseOrder.id == purchase_order_id,
                    PurchaseOrder.organization_id == organization_id,
                    PurchaseOrder.branch_id == branch_id,
                )
            )
            if not purchase_order_exists:
                return PushResult(
                    local_id=record.local_id,
                    table_name="drug_batches",
                    success=False,
                    error="Purchase order does not belong to this sync scope.",
                ), None

        if (
            record.data.get("sync_protocol_version") == 2
            and remaining_quantity != quantity
        ):
            return PushResult(
                local_id=record.local_id,
                table_name="drug_batches",
                success=False,
                error=(
                    "A new protocol-v2 batch must have its full quantity "
                    "available at receipt."
                ),
            ), None

        existing = DrugBatch(**safe)
        existing.sync_status = "synced"
        db.add(existing)
        await db.flush()

        if record.data.get("sync_protocol_version") == 2:
            inventory = (await db.execute(
                select(BranchInventory)
                .where(
                    BranchInventory.branch_id == branch_id,
                    BranchInventory.drug_id == drug_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            previous_quantity = inventory.quantity if inventory else 0
            new_quantity = previous_quantity + quantity
            now = datetime.now(timezone.utc)

            if inventory is None:
                inventory = BranchInventory(
                    id=uuid.uuid4(),
                    branch_id=branch_id,
                    drug_id=drug_id,
                    quantity=new_quantity,
                    reserved_quantity=0,
                    selling_price=safe.get("selling_price"),
                    sync_status="synced",
                    sync_version=1,
                    synced_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(inventory)
            else:
                inventory.quantity = new_quantity
                if (
                    inventory.selling_price is None
                    and safe.get("selling_price") is not None
                ):
                    inventory.selling_price = safe["selling_price"]
                inventory.sync_version += 1
                inventory.sync_status = "synced"
                inventory.synced_at = now
                inventory.updated_at = now

            db.add(StockAdjustment(
                id=uuid.uuid4(),
                branch_id=branch_id,
                drug_id=drug_id,
                adjustment_type="purchase_receipt",
                quantity_change=quantity,
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                reason=f"Offline batch receipt {existing.batch_number}",
                adjusted_by=pushed_by,
                created_at=now,
                updated_at=now,
            ))
            await InventoryService._record_inventory_movement(
                db=db,
                organization_id=organization_id,
                branch_id=branch_id,
                drug_id=drug_id,
                movement_type="purchase_receipt",
                quantity_change=quantity,
                quantity_before=previous_quantity,
                quantity_after=new_quantity,
                batch_id=existing.id,
                batch_quantity_before=0,
                batch_quantity_after=remaining_quantity,
                unit_cost=(
                    Decimal(str(existing.cost_price))
                    if existing.cost_price is not None
                    else None
                ),
                unit_price=(
                    Decimal(str(existing.selling_price))
                    if existing.selling_price is not None
                    else None
                ),
                source_type=(
                    "purchase_order" if purchase_order_id else "batch"
                ),
                source_id=purchase_order_id or existing.id,
                source_line_id=existing.id,
                reference_number=existing.batch_number,
                reason=f"Offline batch receipt {existing.batch_number}",
                created_by=pushed_by,
            )

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
            select(StockAdjustment).where(
                StockAdjustment.id == record.local_id,
                StockAdjustment.branch_id == branch_id,
            )
        )).scalar_one_or_none()

        if existing:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                server_id=str(existing.id),
                success=True,
            ), None

        other_branch_adjustment = await db.scalar(
            select(StockAdjustment.id).where(
                StockAdjustment.id == record.local_id,
                StockAdjustment.branch_id != branch_id,
            )
        )
        if other_branch_adjustment:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error="Stock adjustment does not belong to this sync branch.",
            ), None

        if record.operation != "create":
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error="Stock adjustments are immutable.",
            ), None

        # Whitelist and validate the immutable adjustment data.
        safe = _whitelist(record.data, _ADJUSTMENT_WRITABLE)
        _parse_datetime_fields(safe)

        try:
            quantity_change = int(safe.get("quantity_change", 0))
        except (TypeError, ValueError):
            quantity_change = 0
        if quantity_change == 0 or abs(quantity_change) > MAX_ADJUSTMENT_CHANGE:
            logger.warning(
                "Sync: Rejected stock_adjustment %s with quantity_change=%d "
                "(max allowed: %d)",
                record.local_id, quantity_change, MAX_ADJUSTMENT_CHANGE,
            )
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error=(
                    "Quantity change must be non-zero and no greater than "
                    f"{MAX_ADJUSTMENT_CHANGE} units."
                ),
            ), None

        drug_id = safe.get("drug_id")
        if not drug_id:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error="Missing drug_id.",
            ), None
        drug_exists = await db.scalar(
            select(Drug.id).where(
                Drug.id == drug_id,
                Drug.organization_id == organization_id,
            )
        )
        if not drug_exists:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error=f"Drug {drug_id} not found in organization.",
            ), None

        adjustment_type = str(safe.get("adjustment_type") or "")
        if adjustment_type not in {
            "damage",
            "expired",
            "theft",
            "return",
            "correction",
        }:
            return PushResult(
                local_id=record.local_id,
                table_name="stock_adjustments",
                success=False,
                error=(
                    "Offline adjustments support damage, expired, theft, "
                    "return, and correction only."
                ),
            ), None

        # Reuse the authoritative inventory service so batch quantities,
        # aggregate inventory, adjustment audit, and movement ledger remain
        # in parity inside the current per-record savepoint.
        adj, inventory = await InventoryService._apply_adjustment(
            db=db,
            branch_id=branch_id,
            drug_id=uuid.UUID(str(drug_id)),
            quantity_change=quantity_change,
            adjustment_type=adjustment_type,
            reason=str(safe.get("reason") or "Offline stock adjustment"),
            adjusted_by=pushed_by,
            adjustment_id=uuid.UUID(record.local_id),
        )
        inventory.mark_as_synced()

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
        if not drug_id:
            return PushResult(
                local_id=record.local_id, table_name="branch_inventory", success=False,
                error="Missing drug_id.",
            ), None

        # Validate drug belongs to this organization
        drug_exists = await db.scalar(
            select(Drug.id).where(
                Drug.id == drug_id,
                Drug.organization_id == organization_id,
            )
        )
        if not drug_exists:
            return PushResult(
                local_id=record.local_id, table_name="branch_inventory", success=False,
                error=f"Drug {drug_id} not found in organization.",
            ), None

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
            _parse_datetime_fields(safe)
            safe.pop("id", None)
            safe.pop("drug_id", None)
        else:
            other_branch_inventory = await db.scalar(
                select(BranchInventory.id).where(
                    BranchInventory.id == record.local_id,
                    BranchInventory.branch_id != branch_id,
                )
            )
            if other_branch_inventory:
                return PushResult(
                    local_id=record.local_id,
                    table_name="branch_inventory",
                    success=False,
                    error="Inventory record does not belong to this sync branch.",
                ), None
            safe = _whitelist(record.data, _INVENTORY_WRITABLE)
            _parse_datetime_fields(safe)
            safe["id"] = record.local_id
            safe["branch_id"] = str(branch_id)

        try:
            quantity = int(safe.get(
                "quantity",
                existing.quantity if existing else 0,
            ))
            reserved_quantity = int(safe.get(
                "reserved_quantity",
                existing.reserved_quantity if existing else 0,
            ))
        except (TypeError, ValueError):
            return PushResult(
                local_id=record.local_id,
                table_name="branch_inventory",
                success=False,
                error="Inventory quantities must be valid integers.",
            ), None
        if (
            quantity < 0
            or quantity > MAX_INVENTORY_QUANTITY
            or reserved_quantity < 0
            or reserved_quantity > quantity
        ):
            return PushResult(
                local_id=record.local_id,
                table_name="branch_inventory",
                success=False,
                error="Inventory quantities are outside the accepted range.",
            ), None
        safe["quantity"] = quantity
        safe["reserved_quantity"] = reserved_quantity
        safe["updated_at"] = datetime.now(timezone.utc)

        if existing:
            for k, v in safe.items():
                setattr(existing, k, v)
            existing.sync_status = "synced"
            existing.sync_version += 1
        else:
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
        """Create or update a draft purchase order and its line items."""
        existing = (await db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.id              == record.local_id,
                PurchaseOrder.organization_id == organization_id,
                PurchaseOrder.branch_id       == branch_id,
            )
        )).scalar_one_or_none()

        if existing:
            if existing.status != "draft":
                return PushResult(
                    local_id=record.local_id,
                    table_name="purchase_orders",
                    success=False,
                    error="Only draft purchase orders can be edited offline.",
                ), None
            conflict = SyncService._check_conflict(existing, record, "purchase_orders")
            if conflict:
                return PushResult(
                    local_id=record.local_id, table_name="purchase_orders", success=False
                ), conflict
        else:
            other_branch_po = await db.scalar(
                select(PurchaseOrder.id).where(
                    PurchaseOrder.id == record.local_id,
                    PurchaseOrder.organization_id == organization_id,
                    PurchaseOrder.branch_id != branch_id,
                )
            )
            if other_branch_po:
                return PushResult(
                    local_id=record.local_id,
                    table_name="purchase_orders",
                    success=False,
                    error="Purchase order does not belong to this sync branch.",
                ), None
        safe = _whitelist(record.data, _PO_WRITABLE)
        _parse_datetime_fields(safe)

        supplier_id = safe.get("supplier_id")
        supplier_exists = await db.scalar(
            select(Supplier.id).where(
                Supplier.id == supplier_id,
                Supplier.organization_id == organization_id,
                Supplier.is_deleted == False,
            )
        )
        if not supplier_exists:
            return PushResult(
                local_id=record.local_id,
                table_name="purchase_orders",
                success=False,
                error=f"Supplier {supplier_id} not found in organization.",
            ), None

        items_data = record.data.get("items")
        if not isinstance(items_data, list) or not items_data:
            return PushResult(
                local_id=record.local_id,
                table_name="purchase_orders",
                success=False,
                error="A purchase order must contain at least one item.",
            ), None

        normalized_items: list[dict[str, Any]] = []
        drug_ids: set[uuid.UUID] = set()
        subtotal = Decimal("0")
        for item in items_data:
            if not isinstance(item, dict):
                return PushResult(
                    local_id=record.local_id,
                    table_name="purchase_orders",
                    success=False,
                    error="Every purchase order item must be an object.",
                ), None
            try:
                drug_id = uuid.UUID(str(item.get("drug_id")))
                quantity = int(item.get("quantity_ordered", 0))
                unit_cost = Decimal(str(item.get("unit_cost", 0)))
                item_id = (
                    uuid.UUID(str(item["id"]))
                    if item.get("id")
                    else uuid.uuid4()
                )
            except (ArithmeticError, TypeError, ValueError):
                return PushResult(
                    local_id=record.local_id,
                    table_name="purchase_orders",
                    success=False,
                    error="Purchase order item values are invalid.",
                ), None
            if quantity <= 0 or not unit_cost.is_finite() or unit_cost <= 0:
                return PushResult(
                    local_id=record.local_id,
                    table_name="purchase_orders",
                    success=False,
                    error="Purchase order quantities and unit costs must be positive.",
                ), None
            line_total = unit_cost * quantity
            drug_ids.add(drug_id)
            normalized_items.append({
                "id": item_id,
                "drug_id": drug_id,
                "quantity_ordered": quantity,
                "quantity_received": 0,
                "unit_cost": unit_cost,
                "total_cost": line_total,
                "batch_number": None,
                "expiry_date": None,
            })
            subtotal += line_total

        found_drug_ids = set((await db.execute(
            select(Drug.id).where(
                Drug.id.in_(drug_ids),
                Drug.organization_id == organization_id,
                Drug.is_deleted == False,
            )
        )).scalars().all())
        if found_drug_ids != drug_ids:
            return PushResult(
                local_id=record.local_id,
                table_name="purchase_orders",
                success=False,
                error=f"Drugs not found in organization: {drug_ids - found_drug_ids}",
            ), None

        try:
            tax_amount = Decimal(str(safe.get("tax_amount") or 0))
            shipping_cost = Decimal(str(safe.get("shipping_cost") or 0))
        except (ArithmeticError, TypeError, ValueError):
            tax_amount = shipping_cost = Decimal("-1")
        if (
            not tax_amount.is_finite()
            or not shipping_cost.is_finite()
            or tax_amount < 0
            or shipping_cost < 0
        ):
            return PushResult(
                local_id=record.local_id,
                table_name="purchase_orders",
                success=False,
                error="Tax and shipping amounts cannot be negative.",
            ), None

        safe.update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "shipping_cost": shipping_cost,
            "total_amount": subtotal + tax_amount + shipping_cost,
            "status": "draft",
            "updated_at": datetime.now(timezone.utc),
        })

        if existing:
            safe.pop("id", None)
            safe.pop("created_at", None)
            for k, v in safe.items():
                setattr(existing, k, v)
            existing.sync_status = "synced"
            existing.sync_version += 1
            await db.execute(
                delete(PurchaseOrderItem).where(
                    PurchaseOrderItem.purchase_order_id == existing.id
                )
            )
        else:
            safe["id"] = record.local_id
            safe["organization_id"] = str(organization_id)
            safe["branch_id"] = str(branch_id)
            safe["ordered_by"] = str(pushed_by)
            existing = PurchaseOrder(**safe)
            existing.sync_status = "synced"
            db.add(existing)
            await db.flush()

        for item in normalized_items:
            db.add(PurchaseOrderItem(
                **item,
                purchase_order_id=existing.id,
            ))

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
            select(Customer).where(
                Customer.id == record.local_id,
                Customer.organization_id == organization_id,
            )
        )).scalar_one_or_none()

        if existing and record.operation == "create":
            return PushResult(
                local_id=record.local_id,
                table_name="customers",
                server_id=str(existing.id),
                success=True,
            ), None

        if existing:
            conflict = SyncService._check_conflict(existing, record, "customers")
            if conflict:
                return PushResult(
                    local_id=record.local_id,
                    table_name="customers",
                    success=False,
                ), conflict

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
                    Customer.id != record.local_id,
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
        _parse_datetime_fields(safe)
        safe["updated_at"] = datetime.now(timezone.utc)

        insurance_provider_id = safe.get("insurance_provider_id")
        if insurance_provider_id:
            provider_exists = await db.scalar(
                select(InsuranceProvider.id).where(
                    InsuranceProvider.id == insurance_provider_id,
                    InsuranceProvider.organization_id == organization_id,
                    InsuranceProvider.is_deleted == False,
                )
            )
            if not provider_exists:
                return PushResult(
                    local_id=record.local_id,
                    table_name="customers",
                    success=False,
                    error="Insurance provider does not belong to this organization.",
                ), None

        preferred_contract_id = safe.get("preferred_contract_id")
        if preferred_contract_id:
            contract_exists = await db.scalar(
                select(PriceContract.id).where(
                    PriceContract.id == preferred_contract_id,
                    PriceContract.organization_id == organization_id,
                    PriceContract.is_deleted == False,
                )
            )
            if not contract_exists:
                return PushResult(
                    local_id=record.local_id,
                    table_name="customers",
                    success=False,
                    error="Preferred contract does not belong to this organization.",
                ), None

        if existing:
            safe.pop("id", None)
            for key, value in safe.items():
                setattr(existing, key, value)
            existing.sync_status = "synced"
            existing.sync_version += 1
            customer = existing
        else:
            safe["id"] = record.local_id
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

    @staticmethod
    async def _push_prescription(
        db: AsyncSession,
        record: PushRecord,
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
        pushed_by: uuid.UUID,
    ) -> Tuple[PushResult, Optional[PushConflict]]:
        """Create or update an offline prescription with version checks."""
        existing = (await db.execute(
            select(Prescription).where(
                Prescription.organization_id == organization_id,
                Prescription.id == record.local_id,
            )
        )).scalar_one_or_none()

        if existing and existing.branch_id != branch_id:
            return PushResult(
                local_id=record.local_id,
                table_name="prescriptions",
                success=False,
                error="Prescription does not belong to this sync branch.",
            ), None

        if existing and record.operation == "create":
            return PushResult(
                local_id=record.local_id,
                table_name="prescriptions",
                server_id=str(existing.id),
                success=True,
            ), None

        if existing:
            conflict = SyncService._check_conflict(
                existing,
                record,
                "prescriptions",
            )
            if conflict:
                return PushResult(
                    local_id=record.local_id,
                    table_name="prescriptions",
                    success=False,
                ), conflict
        else:
            duplicate = (await db.execute(
                select(Prescription).where(
                    Prescription.organization_id == organization_id,
                    Prescription.prescription_number
                    == record.data.get("prescription_number"),
                )
            )).scalar_one_or_none()
            if duplicate:
                return PushResult(
                    local_id=record.local_id,
                    table_name="prescriptions",
                    success=False,
                ), PushConflict(
                    local_id=record.local_id,
                    table_name="prescriptions",
                    local_version=record.sync_version,
                    server_version=duplicate.sync_version,
                    server_record=PrescriptionSyncResponse.model_validate(
                        duplicate
                    ).model_dump(mode="json"),
                    resolution="server_wins",
                )

        safe = _whitelist(record.data, _PRESCRIPTION_WRITABLE)
        _parse_datetime_fields(safe)
        safe["updated_at"] = datetime.now(timezone.utc)

        customer_id = safe.get("customer_id")
        customer_exists = await db.scalar(
            select(Customer.id).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
            )
        )
        if not customer_exists:
            return PushResult(
                local_id=record.local_id,
                table_name="prescriptions",
                success=False,
                error=f"Customer {customer_id} not found in organization.",
            ), None

        # Validate branch_id if an older/newer client sends it, then stamp the
        # authenticated sync branch so ownership never depends on client data.
        safe_branch_id = _uuid_or_none(safe.get("branch_id"))
        if safe_branch_id and safe_branch_id != branch_id:
            return PushResult(
                local_id=record.local_id,
                table_name="prescriptions",
                success=False,
                error=f"Branch mismatch: prescription belongs to {safe_branch_id}, pushing from {branch_id}.",
            ), None
        safe["branch_id"] = str(branch_id)

        # Ensure medications is correctly handled (it's JSONB in DB)
        if "medications" in safe and isinstance(safe["medications"], str):
            import json
            try:
                safe["medications"] = json.loads(safe["medications"])
            except (TypeError, ValueError):
                return PushResult(
                    local_id=record.local_id,
                    table_name="prescriptions",
                    success=False,
                    error="medications must be a valid JSON array.",
                ), None

        if existing:
            safe.pop("id", None)
            for key, value in safe.items():
                setattr(existing, key, value)
            existing.sync_status = "synced"
            existing.sync_version += 1
            prescription = existing
        else:
            safe["id"] = record.local_id
            safe["organization_id"] = str(organization_id)
            prescription = Prescription(**safe)
            prescription.sync_status = "synced"
            db.add(prescription)
        await db.flush()

        return PushResult(
            local_id=record.local_id,
            table_name="prescriptions",
            server_id=str(prescription.id),
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
        Return a ``PushConflict`` unless the client presents the next version.
        Equality means another writer already committed that version; exact
        retries are intercepted by operation receipts before this method.
        """
        server_version = getattr(server_record, "sync_version", 1)
        if client_record.force:
            return None

        # The client increments its version before queueing an update. Equality
        # therefore means another writer already committed that same next
        # version. Replays of the same mutation are handled by operation
        # receipts before reaching this comparison.
        if server_version < client_record.sync_version:
            return None

        resolution = CONFLICT_RESOLUTION.get(table_name, "server_wins")

        _schema_map = {
            "branch_inventory": BranchInventoryResponse,
            "drug_batches":     DrugBatchResponse,
            "purchase_orders":  PurchaseOrderResponse,
            "sales":            SaleResponse,
            "customers":        CustomerResponse,
            "prescriptions":    PrescriptionSyncResponse,
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
