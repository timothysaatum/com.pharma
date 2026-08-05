"""
CRR Sync API Router
===================
Mounts at /api/v1/sync (under the sync router)

  POST /sync/crr-push  — branch pushes crsql_changes rows for CRR tables
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user, get_db
from app.models.pharmacy.pharmacy_model import Branch
from app.models.user.user_model import User
from app.schemas.sync_schemas import (
    CrrPushRequest,
    CrrPushResponse,
    PullRequest,
    PullResponse,
)
from app.services.sync.crr_sync_service import CrrSyncService
from app.services.sync.shadow_db import get_shadow_db

router = APIRouter(prefix="/sync", tags=["Offline Sync (CRDT)"])


def _user_can_sync_branch(current_user: User, branch_id) -> bool:
    assigned = {str(value) for value in (current_user.assigned_branches or [])}
    return str(branch_id) in assigned


async def _require_crr_shadow():
    shadow = await get_shadow_db()
    if not shadow.crr_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "CRR synchronization is temporarily unavailable because the "
                "server cr-sqlite extension is not loaded."
            ),
            headers={"Retry-After": "60"},
        )
    return shadow


@router.post(
    "/crr-push",
    response_model=CrrPushResponse,
    summary="Push crsql_changes rows for CRR-migrated tables",
    description="""
Branch pushes raw cr-sqlite change rows for tables that have been
migrated to CRDT-based conflict resolution (currently: branch_inventory).

The server inserts these into its shadow SQLite database, lets cr-sqlite's
triggers auto-merge the changes, validates the merged result against
existing business rules (FK checks, quantity ranges), and upserts the
final merged state into Postgres.

This endpoint runs alongside the existing /sync/push endpoint for
tables that have NOT yet been migrated to CRR.
""",
)
async def crr_push(
    request: CrrPushRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> CrrPushResponse:
    if not _user_can_sync_branch(current_user, request.branch_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    await _require_crr_shadow()

    response = await CrrSyncService.handle_crr_push(
        db=db,
        changes=request.changes,
        organization_id=current_user.organization_id,
        branch_id=request.branch_id,
    )
    accepted, audit_errors = await CrrSyncService.persist_client_renumber_audits(
        db=db,
        events=request.audit_events,
        organization_id=current_user.organization_id,
        branch_id=request.branch_id,
    )
    response.accepted_audit_event_ids = accepted
    response.audit_errors = audit_errors
    return response


@router.post(
    "/crr-pull",
    response_model=PullResponse,
    summary="Pull CRR crsql_changes delta from server",
    description="""
Returns cr-sqlite crsql_changes rows from the server's shadow database
since the client's last known db_version (crr_since_db_version).

The client inserts these rows into its local crsql_changes table,
which triggers cr-sqlite's automatic CRDT merge into local tables.

This endpoint can be called alongside the existing /sync/pull for
non-CRR tables.  For migrated tables, the branch should NOT pull
data through the old /sync/pull endpoint — it will get inconsistent
results because those tables' sync is now managed by cr-sqlite.
""",
)
async def crr_pull(
    request: PullRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PullResponse:
    if not _user_can_sync_branch(current_user, request.branch_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    shadow = await _require_crr_shadow()

    published = await shadow.publish_server_authoritative_tables(
        db=db,
        organization_id=current_user.organization_id,
        branch_id=request.branch_id,
    )
    if published:
        await db.commit()

    max_db = await shadow.max_db_version()

    # Every table's ownership is resolved to this org either directly (most
    # tables carry organization_id) or via branch membership (branch_inventory
    # / drug_batches, which only carry branch_id) — see S1 in
    # docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md.
    org_branch_ids = (
        await db.execute(
            select(Branch.id).where(Branch.organization_id == current_user.organization_id)
        )
    ).scalars().all()

    changes = await shadow.get_changes_since(
        organization_id=str(current_user.organization_id),
        org_branch_ids=[str(b) for b in org_branch_ids],
        since_db_version=request.crr_since_db_version,
    )

    crr_max = max((c.get("db_version", 0) for c in changes), default=0)
    directive_result = await db.execute(text("""
        SELECT id, event_id, survivor_id, loser_id, merged_at
        FROM crr_customer_merge_audit
        WHERE organization_id = :organization_id
          AND id > :since_version
        ORDER BY id
        LIMIT 500
    """), {
        "organization_id": str(current_user.organization_id),
        "since_version": request.customer_merge_since_version,
    })
    directive_rows = directive_result.mappings().all()
    directive_max = max(
        (int(row["id"]) for row in directive_rows),
        default=request.customer_merge_since_version,
    )

    return PullResponse(
        crr_changes=[
            {
                "table": c["table"],
                "pk": c["pk"],
                "cid": c["cid"],
                "val": c["val"],
                "col_version": c["col_version"],
                "db_version": c["db_version"],
                "site_id": c["site_id"],
                "cl": c["cl"],
                "seq": c["seq"],
            }
            for c in changes
        ],
        crr_max_db_version=crr_max,
        customer_merge_directives=[
            {
                "directive_version": row["id"],
                "event_id": row["event_id"],
                "survivor_id": row["survivor_id"],
                "loser_id": row["loser_id"],
                "merged_at": row["merged_at"],
            }
            for row in directive_rows
        ],
        customer_merge_max_version=directive_max,
        sync_timestamp=datetime.now(timezone.utc),
        has_more=len(changes) >= 500 or len(directive_rows) >= 500,
        total_records=len(changes) + len(directive_rows),
    )
