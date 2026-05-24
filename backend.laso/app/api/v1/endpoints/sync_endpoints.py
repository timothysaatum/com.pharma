"""
Sync API Router
===============
Mounts at /api/v1/sync

  POST /sync/pull   — branch pulls delta from server
  POST /sync/push   — branch pushes pending records to server

Add to your v1 router:
    from app.api.v1.sync_endpoints import router as sync_router
    v1_router.include_router(sync_router)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user, get_db
from app.models.user.user_model import User
from app.schemas.sync_schemas import (
    PullRequest, PullResponse,
    PushRequest, PushResponse,
)
from app.services.sync.sync_service import SyncService

router = APIRouter(prefix="/sync", tags=["Offline Sync"])


@router.post(
    "/pull",
    response_model=PullResponse,
    summary="Pull delta from server",
    description="""
Branch calls this after reconnecting (or on a timer while online).

**First sync:** omit `last_sync_at` to receive the complete dataset.

**Subsequent syncs:** pass the `sync_timestamp` returned by the previous
pull as `last_sync_at`. Only records updated after that timestamp are returned.

**Org-level tables** (drugs, categories, contracts, customers) are
returned for the whole organisation — branches can only read these.

**Branch-level tables** (inventory, batches, sales, purchase_orders)
are filtered to the requesting branch only.

Store the returned `sync_timestamp` locally and use it as `last_sync_at`
on the next pull.
""",
)
async def pull(
    request: PullRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PullResponse:
    # Verify the user has access to the requested branch
    if str(request.branch_id) not in (current_user.assigned_branches or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    return await SyncService.pull(
        db=db,
        request=request,
        organization_id=current_user.organization_id,
    )


@router.post(
    "/push",
    response_model=PushResponse,
    summary="Push pending records to server",
    description="""
Branch pushes records that were created or modified while offline.

**Pushable tables:** `sales`, `drug_batches`, `stock_adjustments`,
`branch_inventory`, `purchase_orders`, `customers`.

**Read-only tables** (drugs, price_contracts, etc.) cannot be pushed —
the server will reject them with a validation error.

**Conflict handling:**
- `sales`, `inventory`, `batches`, `adjustments`, `purchase_orders`
  → `server_wins` (server is authoritative after sync)
- `customers` → `manual_required` when a duplicate phone/email is found

**Idempotency:** safe to re-push the same records after a network failure.
The server deduplicates by record ID.

After a successful push, immediately call `/sync/pull` with the returned
`next_pull_timestamp` as `last_sync_at` to receive any server-side
changes triggered by your push (e.g. updated inventory totals).
""",
)
async def push(
    request: PushRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PushResponse:
    if str(request.branch_id) not in (current_user.assigned_branches or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    response = await SyncService.push(
        db=db,
        request=request,
        organization_id=current_user.organization_id,
        pushed_by=current_user.id,
    )

    # Commit all accepted records in one transaction
    await db.commit()

    return response


@router.get(
    "/status",
    summary="Server sync status",
    description="Returns the current server timestamp. "
                "Useful for the client to calibrate its clock before syncing.",
)
async def sync_status(
    current_user: User = Depends(get_current_active_user),
) -> dict:
    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "organization_id": str(current_user.organization_id),
        "user_id": str(current_user.id),
    }