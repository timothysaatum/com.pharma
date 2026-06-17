"""
Audit Log API Endpoints
======================
Allows administrators and managers to view user activity and system changes.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import uuid

from app.core.deps import get_current_user, require_role
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.models.system_md.sys_models import AuditLog
from app.schemas.syst_schemas import AuditLogResponse, PaginationParams
from app.utils.pagination import PaginatedResponse, Paginator

router = APIRouter(prefix="/audit", tags=["Audit Logs"])

@router.get(
    "/",
    response_model=PaginatedResponse[AuditLogResponse],
    dependencies=[Depends(require_role(["admin", "manager", "super_admin"]))]
)
async def list_audit_logs(
    pagination: PaginationParams = Depends(),
    user_id: Optional[uuid.UUID] = Query(None),
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List audit logs for the organization.

    **Permissions:** admin, manager, super_admin
    """
    from sqlalchemy.orm import joinedload

    query = (
        select(AuditLog)
        .options(joinedload(AuditLog.user))
        .where(AuditLog.organization_id == current_user.organization_id)
        .order_by(AuditLog.created_at.desc())
    )

    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if action:
        query = query.where(AuditLog.action == action)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)

    paginator = Paginator(db)
    result = await paginator.paginate(query, pagination)

    items = []
    for log in result.items:
        items.append(
            AuditLogResponse(
                id=log.id,
                organization_id=log.organization_id,
                user_id=log.user_id,
                user_full_name=log.user.full_name if log.user else "System",
                action=log.action,
                entity_type=log.entity_type,
                entity_id=log.entity_id,
                changes=log.changes,
                ip_address=log.ip_address,
                user_agent=log.user_agent,
                context_metadata=log.context_metadata,
                created_at=log.created_at,
            )
        )

    return PaginatedResponse(
        items=items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        total_pages=result.total_pages,
        has_next=result.has_next,
        has_prev=result.has_prev,
    )
