from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import uuid

from app.models.system_md.sys_models import AuditLog
from app.core.config import get_settings

settings = get_settings()


class AuditService:
    """Service for creating audit log entries"""

    @staticmethod
    async def log(
        db: AsyncSession,
        organization_id: uuid.UUID,
        action: str,
        user_id: Optional[uuid.UUID] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[uuid.UUID] = None,
        changes: Optional[dict] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        context_metadata: Optional[dict] = None,
    ) -> None:
        if not settings.AUDIT_LOG_ENABLED:
            return

        entry = AuditLog(
            id=uuid.uuid4(),
            organization_id=organization_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=changes or {},
            ip_address=ip_address,
            user_agent=user_agent,
            context_metadata=context_metadata or {},
            created_at=datetime.now(timezone.utc),
        )
        db.add(entry)
