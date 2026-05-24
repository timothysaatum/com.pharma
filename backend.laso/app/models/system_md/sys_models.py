from app.db.base import Base

from sqlalchemy.sql import func
from sqlalchemy import (
    String, Integer, Boolean, DateTime, Text,
    ForeignKey, Index, CheckConstraint, BigInteger, event, text
)
from app.models.db_types import UUID, ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
import uuid

from app.models.core.mixins import TimestampMixin
if TYPE_CHECKING:
    from app.models.inventory.inventory_model import Drug
    from app.models.user.user_model import User

class AuditLog(Base):
    """
    Comprehensive audit trail for all critical operations.
    Immutable - no updates or deletes allowed.
    """
    __tablename__ = 'audit_logs'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        index=True
    )
    
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Action performed: create, update, delete, login, etc."
    )
    
    entity_type: Mapped[Optional[str]] = mapped_column(
        String(100),
        index=True,
        comment="Table/model affected"
    )
    
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        index=True,
        comment="ID of affected record"
    )
    
    # Detailed change tracking
    changes: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="{ before: {...}, after: {...} }"
    )
    
    # Request metadata
    ip_address: Mapped[Optional[str]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    
    # Additional context
    context_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="Additional context about the action"
    )
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )
    
    # Relationships
    user: Mapped[Optional["User"]] = relationship(back_populates="audit_logs")
    
    __table_args__ = (
        Index('idx_audit_org', 'organization_id'),
        Index('idx_audit_user', 'user_id'),
        Index('idx_audit_action', 'action'),
        Index('idx_audit_entity', 'entity_type', 'entity_id'),
        Index('idx_audit_date', 'created_at'),
        # Partitioning by month recommended for large datasets
    )


class SystemAlert(Base, TimestampMixin):
    """
    System-generated alerts for low stock, expiry, etc.
    """
    __tablename__ = 'system_alerts'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('branches.id', ondelete='CASCADE'),
        nullable=True,
        index=True
    )
    
    alert_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="low_stock, expiry_warning, out_of_stock, system_error, security, system_info"
    )
    
    severity: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        comment="low, medium, high, critical"
    )
    
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Related entities
    drug_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drugs.id', ondelete='CASCADE')
    )
    
    # Resolution tracking
    is_resolved: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )
    
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL')
    )
    
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Notification tracking
    notifications_sent: Mapped[List[str]] = mapped_column(
        ARRAY(String),
        default=list,
        comment="List of notification channels used: email, sms, push"
    )
    
    __table_args__ = (
        CheckConstraint(
            "alert_type IN ('low_stock', 'expiry_warning', 'out_of_stock', 'system_error', 'security', 'system_info')",
            name='check_alert_type'
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name='check_alert_severity'
        ),
        Index('idx_alert_org', 'organization_id'),
        Index('idx_alert_branch', 'branch_id'),
        Index('idx_alert_type', 'alert_type'),
        Index('idx_alert_severity', 'severity'),
        Index('idx_alert_resolved', 'is_resolved'),
        Index('idx_alert_unresolved', 'organization_id', 'is_resolved', 
              postgresql_where=text('is_resolved = false')),
    )



class SyncQueue(Base):
    """
    Queue for pending synchronization operations.
    Critical for offline-first architecture.
    """
    __tablename__ = 'sync_queue'
    
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )
    
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    operation: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="create, update, delete"
    )
    
    table_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True
    )
    
    record_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True
    )
    
    data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Full record data as JSON"
    )
    
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Higher priority processed first"
    )
    
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    
    status: Mapped[str] = mapped_column(
        String(20),
        default='pending',
        nullable=False,
        index=True,
        comment="pending, processing, failed, completed"
    )
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )
    
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        index=True,
        comment="For retry backoff"
    )
    
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    __table_args__ = (
        CheckConstraint(
            "operation IN ('create', 'update', 'delete')",
            name='check_sync_operation'
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'failed', 'completed')",
            name='check_sync_status'
        ),
        Index('idx_sync_queue_status', 'status', 'priority'),
        Index('idx_sync_queue_org', 'organization_id'),
        Index('idx_sync_queue_scheduled', 'scheduled_at'),
        Index('idx_sync_queue_pending', 'status', 'scheduled_at',
              postgresql_where=text("status = 'pending'")),
    )