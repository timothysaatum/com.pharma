"""
CRR Sync Service
================

Handles cr-sqlite CRDT-based sync for tables migrated to CRR.

Per ADR 0003:
  1. Client POSTs crsql_changes rows to /sync/crr-push
  2. Server validates (org scope, FK checks)
  3. Server INSERTs into shadow crsql_changes → triggers auto-merge
  4. Server reads merged state from shadow table
  5. Server upserts merged row into Postgres

Per ADR 0002: single Mutex<Connection> on the shadow DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer.customer_model import Customer
from app.models.inventory.inventory_model import Drug
from app.schemas.sync_schemas import (
    CrrPushRecord,
    CrrRenumberAuditEvent,
    CrrPushResult,
    CrrPushResponse,
)
from app.services.sync.shadow_db import get_crr_config, get_shadow_db

logger = logging.getLogger(__name__)


# ── Table-level FK / validation rules ─────────────────────────────────
#
# Every pushable (non-server-authoritative) CRR table MUST have a validator
# here that at minimum enforces tenant ownership. Without one, a merged row
# is upserted into Postgres via ON CONFLICT (id) DO UPDATE with whatever
# organization_id/branch_id the client embedded — i.e. a client could
# overwrite another organization's row by referencing its id. This closes
# S2 in docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md.

# Tables that are FK-validated before the merge is accepted
_CRR_VALIDATORS: Dict[str, callable] = {}


def _validate_scope(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    check_organization: bool,
    check_branch: bool,
) -> Optional[str]:
    """
    Reject a merged row whose embedded organization_id/branch_id doesn't
    match the authenticated push's scope.

    The branch_id passed in here is the request's branch_id, which the
    endpoint already verified is one the pushing user is assigned to
    (``_user_can_sync_branch``) — so requiring an exact match also transitively
    enforces organization ownership for tables that only carry branch_id
    (branch_inventory, drug_batches), which have no organization_id column
    of their own to check.
    """
    if check_organization:
        row_org = str(merged.get("organization_id") or "")
        if row_org != str(organization_id):
            return f"organization_id {row_org!r} does not match authenticated organization"
    if check_branch:
        row_branch = str(merged.get("branch_id") or "")
        if row_branch != str(branch_id):
            return f"branch_id {row_branch!r} does not match the branch this push was authorized for"
    return None


async def _validate_branch_inventory(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[str]:
    """Validate a merged branch_inventory row.

    Returns an error string or None if valid.
    """
    scope_error = _validate_scope(
        merged, organization_id, branch_id, check_organization=False, check_branch=True
    )
    if scope_error:
        return scope_error

    drug_id = merged.get("drug_id", "")
    if not drug_id:
        return "Missing drug_id"

    # Drug must belong to this organization
    drug = await db.scalar(
        select(Drug.id).where(
            Drug.id == drug_id,
            Drug.organization_id == organization_id,
        )
    )
    if not drug:
        return f"Drug {drug_id} not found in organization"

    # Quantity sanity
    try:
        qty = int(merged.get("quantity", 0))
        reserved = int(merged.get("reserved_quantity", 0))
    except (TypeError, ValueError):
        return "Invalid quantity values"

    if qty < 0 or qty > 1_000_000:
        return f"Quantity {qty} out of range"
    if reserved < 0 or reserved > qty:
        return f"reserved_quantity {reserved} out of range for quantity {qty}"

    return None


async def _validate_drug_batch(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[str]:
    """Validate a merged drug_batches row (branch-scoped, no organization_id column)."""
    scope_error = _validate_scope(
        merged, organization_id, branch_id, check_organization=False, check_branch=True
    )
    if scope_error:
        return scope_error

    drug_id = merged.get("drug_id", "")
    if not drug_id:
        return "Missing drug_id"

    drug = await db.scalar(
        select(Drug.id).where(
            Drug.id == drug_id,
            Drug.organization_id == organization_id,
        )
    )
    if not drug:
        return f"Drug {drug_id} not found in organization"

    try:
        qty = int(merged.get("quantity", 0))
        remaining = int(merged.get("remaining_quantity", 0))
    except (TypeError, ValueError):
        return "Invalid quantity values"

    if qty < 0 or remaining < 0:
        return "Batch quantities cannot be negative"
    if remaining > qty:
        return f"remaining_quantity {remaining} cannot exceed quantity {qty}"

    return None


async def _validate_customer(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[str]:
    """Validate a merged customers row (organization-scoped, no branch_id column)."""
    return _validate_scope(
        merged, organization_id, branch_id, check_organization=True, check_branch=False
    )


async def _validate_prescription(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[str]:
    """Validate a merged prescriptions row."""
    scope_error = _validate_scope(
        merged, organization_id, branch_id, check_organization=True, check_branch=True
    )
    if scope_error:
        return scope_error

    customer_id = merged.get("customer_id", "")
    if customer_id:
        customer = await db.scalar(
            select(Customer.id).where(
                Customer.id == customer_id,
                Customer.organization_id == organization_id,
            )
        )
        if not customer:
            return f"Customer {customer_id} not found in organization"

    return None


async def _validate_purchase_order(
    merged: Dict[str, Any],
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession,
) -> Optional[str]:
    """Validate a merged purchase_orders row."""
    return _validate_scope(
        merged, organization_id, branch_id, check_organization=True, check_branch=True
    )


_CRR_VALIDATORS["branch_inventory"] = _validate_branch_inventory
_CRR_VALIDATORS["drug_batches"] = _validate_drug_batch
_CRR_VALIDATORS["customers"] = _validate_customer
_CRR_VALIDATORS["prescriptions"] = _validate_prescription
_CRR_VALIDATORS["purchase_orders"] = _validate_purchase_order


# Merge strategies (see shadow_db.py's _CRR_TABLE_CONFIG["strategy"]) that
# call delete_crr_row to retire a losing duplicate's shadow row as part of
# NORMAL, successful operation. Only these strategies make "this pk is
# tombstoned" a safe signal for "already handled, nothing to upsert" —
# keep_both_renumber (prescriptions, purchase_orders, sales) never deletes
# on collision, so a tombstone there can only come from
# restore_rejected_row's validation-rejection path and must keep failing.
_STRATEGIES_WITH_LEGITIMATE_TOMBSTONES = {"sum_and_merge", "lww_with_external_dedup"}


# ── Main service ──────────────────────────────────────────────────────

class CrrSyncService:

    @staticmethod
    async def persist_client_renumber_audits(
        db: AsyncSession,
        events: List[CrrRenumberAuditEvent],
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
    ) -> Tuple[List[str], Dict[str, str]]:
        """Persist client migration audits after verifying both rows are in scope."""
        accepted: List[str] = []
        errors: Dict[str, str] = {}
        expected_columns = {
            "prescriptions": "prescription_number",
            "purchase_orders": "po_number",
            "sales": "sale_number",
        }

        for event in events:
            try:
                expected_column = expected_columns[event.table_name]
                if event.business_key_col != expected_column:
                    raise ValueError(
                        f"Invalid business key column for {event.table_name}"
                    )
                expected_event_id = (
                    f"{event.table_name}:{event.loser_id}:"
                    f"{event.old_business_key}:{event.new_business_key}"
                )
                if event.event_id != expected_event_id:
                    raise ValueError("Audit event_id does not match event contents")

                scoped_rows = await db.execute(
                    text(f"""
                        SELECT id FROM {event.table_name}
                        WHERE id IN (:winner_id, :loser_id)
                          AND organization_id = :organization_id
                          AND branch_id = :branch_id
                    """),
                    {
                        "winner_id": str(event.winner_id),
                        "loser_id": str(event.loser_id),
                        "organization_id": str(organization_id),
                        "branch_id": str(branch_id),
                    },
                )
                ids = {str(row[0]) for row in scoped_rows.fetchall()}
                if ids != {str(event.winner_id), str(event.loser_id)}:
                    raise ValueError("Audit rows are missing or outside sync scope")

                await db.execute(text("""
                    INSERT INTO crr_renumber_audit
                        (event_id, table_name, winner_id, loser_id,
                         business_key_col, old_business_key, new_business_key,
                         renumbered_at)
                    VALUES
                        (:event_id, :table_name, :winner_id, :loser_id,
                         :business_key_col, :old_business_key, :new_business_key,
                         :renumbered_at)
                    ON CONFLICT (event_id) DO NOTHING
                """), {
                    "event_id": event.event_id,
                    "table_name": event.table_name,
                    "winner_id": str(event.winner_id),
                    "loser_id": str(event.loser_id),
                    "business_key_col": event.business_key_col,
                    "old_business_key": event.old_business_key,
                    "new_business_key": event.new_business_key,
                    "renumbered_at": event.renumbered_at,
                })
                accepted.append(event.event_id)
            except Exception as exc:
                logger.warning(
                    "Rejected client renumber audit %s: %s",
                    event.event_id, exc,
                )
                errors[event.event_id] = str(exc)

        await db.commit()
        return accepted, errors

    @staticmethod
    async def handle_crr_push(
        db: AsyncSession,
        changes: List[CrrPushRecord],
        organization_id: uuid.UUID,
        branch_id: uuid.UUID,
    ) -> CrrPushResponse:
        """
        Accept a batch of crsql_changes rows from a client branch.

        For each row:
          1. Validate the table is a known CRR table
          2. Group by table + PK
          3. Insert into shadow crsql_changes (triggers auto-merge)
          4. Read the merged row from the shadow table
          5. Validate merged row (FK, business rules)
          6. If valid, upsert into Postgres

        If validation fails for a group, the shadow DB changes are
        rolled back to the savepoint and the error is reported.
        """
        now = datetime.now(timezone.utc)
        shadow = await get_shadow_db()
        results: List[CrrPushResult] = []
        merged_ids: List[str] = []

        # Group changes by (table, pk)
        groups: Dict[Tuple[str, str], List[CrrPushRecord]] = {}
        for ch in changes:
            pk_str = str(ch.pk) if ch.pk is not None else ""
            key = (ch.table, pk_str)
            groups.setdefault(key, []).append(ch)

        for (table, pk_str), group_changes in groups.items():
            validator = _CRR_VALIDATORS.get(table)
            row_id = pk_str
            error: Optional[str] = None

            try:
                cfg = get_crr_config(table)
                if cfg and cfg.get("server_authoritative"):
                    raise ValueError(
                        f"{table} is server-authoritative and cannot be pushed by clients"
                    )

                # Build the raw tuples for shadow DB insert
                rows_to_insert: List[Tuple[Any, ...]] = []
                for ch in group_changes:
                    rows_to_insert.append((
                        ch.table,
                        ch.pk,
                        ch.cid,
                        ch.val,
                        ch.col_version,
                        ch.db_version,
                        ch.site_id,
                        ch.cl,
                        ch.seq,
                    ))

                # Insert into shadow crsql_changes (auto-merge via triggers)
                await shadow.insert_crr_changes(rows_to_insert)

                resolved_row_id = await shadow.resolve_row_id(
                    table, group_changes[0].pk
                )
                if resolved_row_id is None:
                    # A tombstoned pk is ambiguous — it can mean either
                    # "already legitimately retired" or "rejected by
                    # validation, with nothing to restore" (restore_rejected_row
                    # below deletes a brand-new row's shadow copy on its very
                    # first rejection, leaving an identical tombstone). Only
                    # sum_and_merge/lww_with_external_dedup ever retire a row
                    # as part of normal, successful operation — a
                    # keep_both_renumber table (prescriptions, purchase_orders)
                    # NEVER deletes on collision, so a tombstone there can only
                    # be a prior rejection, and must keep failing, not be
                    # silently reported as success. See pk_is_tombstoned's
                    # docstring for the general mechanism.
                    strategy = (cfg or {}).get("strategy")
                    if (
                        strategy in _STRATEGIES_WITH_LEGITIMATE_TOMBSTONES
                        and await shadow.pk_is_tombstoned(table, group_changes[0].pk)
                    ):
                        results.append(CrrPushResult(
                            table=table,
                            row_id=pk_str,
                            success=True,
                        ))
                        continue
                    error = f"Unable to resolve encoded primary key for {table}"
                    raise ValueError(error)
                row_id = resolved_row_id

                # Read the merged state
                merged = await shadow.get_merged_row(table, row_id)
                if merged is None:
                    error = f"Row {row_id} not found after merge in table {table}"
                elif validator is not None:
                    error = await validator(merged, organization_id, branch_id, db)
                else:
                    # Fail closed: every pushable (non-server-authoritative)
                    # CRR table must have an ownership validator. A table
                    # reaching here with none would upsert into Postgres
                    # with whatever organization_id/branch_id the client
                    # embedded, unchecked — see S2 in
                    # docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md.
                    error = f"No push validator registered for CRR table {table!r}"

                if error:
                    logger.warning(
                        "CRR push validation failed: table=%s row=%s error=%s",
                        table, row_id, error,
                    )
                    authoritative = await db.execute(
                        text(f'SELECT * FROM "{table}" WHERE id = :row_id'),
                        {"row_id": row_id},
                    )
                    authoritative_row = authoritative.mappings().first()
                    await shadow.restore_rejected_row(
                        table,
                        row_id,
                        dict(authoritative_row) if authoritative_row else None,
                    )
                    results.append(CrrPushResult(
                        table=table,
                        row_id=row_id,
                        success=False,
                        error=error,
                    ))
                else:
                    # Upsert the merged row into Postgres
                    # (delegates to shadow.upsert_merged_row which handles
                    # duplicate business-key detection and configurable merge)
                    await shadow.upsert_merged_row(db, table, merged)
                    merged_ids.append(row_id)
                    results.append(CrrPushResult(
                        table=table,
                        row_id=row_id,
                        success=True,
                    ))

            except Exception as exc:
                logger.error(
                    "CRR push error: table=%s row=%s exc=%s",
                    table, row_id, exc, exc_info=True,
                )
                results.append(CrrPushResult(
                    table=table,
                    row_id=row_id,
                    success=False,
                    error=str(exc),
                ))

        await db.commit()

        accepted = len([r for r in results if r.success])
        failed = len([r for r in results if not r.success])

        return CrrPushResponse(
            results=results,
            total_received=len(changes),
            total_accepted=accepted,
            total_failed=failed,
            sync_timestamp=now,
            merged_row_ids=merged_ids,
        )
