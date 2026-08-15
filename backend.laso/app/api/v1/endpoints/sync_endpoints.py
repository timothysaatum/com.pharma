"""
Sync API Router
===============
Mounts at /api/v1/sync

  POST /sync/void-failed-sale — void a permanently failed offline sale
  GET  /sync/status           — server clock status
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_active_user, get_db
from app.models.system_md.sys_models import AuditLog
from app.models.user.user_model import Permission, User
from app.schemas.sync_schemas import (
    VoidFailedSaleRequest, VoidFailedSaleResponse,
)

router = APIRouter(prefix="/sync", tags=["Offline Sync"])


def _user_can_sync_branch(current_user: User, branch_id) -> bool:
    assigned = {str(value) for value in (current_user.assigned_branches or [])}
    return str(branch_id) in assigned


@router.post(
    "/void-failed-sale",
    response_model=VoidFailedSaleResponse,
    summary="Void a sale that permanently failed to sync",
    description="""
Gives up on retrying a dead-lettered offline sale, and records an audited,
manager-approved record of the decision.
""",
)
async def void_failed_sale(
    request: VoidFailedSaleRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> VoidFailedSaleResponse:
    if not _user_can_sync_branch(current_user, request.branch_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    if request.manager_approval_user_id == current_user.id:
        approver = current_user
    else:
        approver_res = await db.execute(
            select(User).where(
                User.id == request.manager_approval_user_id,
                User.organization_id == current_user.organization_id,
            ).options(selectinload(User.roles))
        )
        approver = approver_res.scalar_one_or_none()
        if not approver:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approving manager not found.",
            )

    if not (approver.is_super_admin or approver.has_permission(Permission.PROCESS_REFUNDS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approving manager does not have required permissions.",
        )

    audit_log = AuditLog(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        user_id=current_user.id,
        action="void_unsynced_sale",
        entity_type="Sale",
        entity_id=request.sale_id,
        changes={
            "sale_id": str(request.sale_id),
            "branch_id": str(request.branch_id),
            "sale_number": request.sale_number,
            "total_amount": request.total_amount,
            "reason": request.reason,
            "last_sync_error": request.last_sync_error,
            "sync_attempts": request.sync_attempts,
            "approved_by": str(approver.id),
            "approved_by_name": getattr(approver, "full_name", None) or approver.email,
            "voided_by": str(current_user.id),
        },
        created_at=datetime.now(timezone.utc),
    )
    db.add(audit_log)
    await db.commit()
    await db.refresh(audit_log)

    return VoidFailedSaleResponse(voided=True, audit_log_id=audit_log.id)


@router.get(
    "/status",
    summary="Server sync status",
    description="Returns the current server timestamp.",
)
async def sync_status(
    current_user: User = Depends(get_current_active_user),
) -> dict:
    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "organization_id": str(current_user.organization_id),
        "user_id": str(current_user.id),
    }

