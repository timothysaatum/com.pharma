"""
Audit Log API Endpoints
======================
Allows administrators and managers to view user activity and system changes.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import String, cast, or_, select
from typing import Optional
from datetime import date, datetime, time, timedelta, timezone
import uuid

from app.core.deps import get_current_user, require_permission
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.models.system_md.sys_models import AuditLog
from app.schemas.syst_schemas import AuditLogResponse, PaginationParams
from app.utils.pagination import PaginatedResponse, Paginator

router = APIRouter(prefix="/audit", tags=["Audit Logs"])

@router.get(
    "/",
    response_model=PaginatedResponse[AuditLogResponse],
    dependencies=[Depends(require_permission("view_audit_logs"))]
)
async def list_audit_logs(
    pagination: PaginationParams = Depends(),
    user_id: Optional[uuid.UUID] = Query(None),
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=500),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
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
        .outerjoin(User, AuditLog.user_id == User.id)
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
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(
            or_(
                AuditLog.action.ilike(term),
                AuditLog.entity_type.ilike(term),
                cast(AuditLog.entity_id, String).ilike(term),
                cast(AuditLog.changes, String).ilike(term),
                cast(AuditLog.context_metadata, String).ilike(term),
                cast(AuditLog.ip_address, String).ilike(term),
                AuditLog.user_agent.ilike(term),
                User.full_name.ilike(term),
                User.username.ilike(term),
            )
        )
    if start_date:
        query = query.where(
            AuditLog.created_at
            >= datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        )
    if end_date:
        query = query.where(
            AuditLog.created_at
            < datetime.combine(
                end_date + timedelta(days=1),
                time.min,
                tzinfo=timezone.utc,
            )
        )

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
