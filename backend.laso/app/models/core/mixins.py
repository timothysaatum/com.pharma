
from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey, BigInteger
)
from app.models.db_types import UUID
from sqlalchemy.orm import (
    Mapped, mapped_column, declarative_mixin
)
from sqlalchemy.sql import func
from typing import Optional
from datetime import datetime, timezone
import uuid
from passlib.context import CryptContext
from cryptography.fernet import Fernet
import os

# Password hashing context
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=65536,
    argon2__time_cost=3,
    argon2__parallelism=4
)

# Encryption for sensitive data
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", Fernet.generate_key())
cipher_suite = Fernet(ENCRYPTION_KEY)


@declarative_mixin
class TimestampMixin:
    """Mixin for created_at and updated_at timestamps"""
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )
    
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True
    )


@declarative_mixin
class SyncTrackingMixin:
    """Mixin for offline-first sync tracking"""
    
    sync_version: Mapped[int] = mapped_column(
        BigInteger,
        default=1,
        nullable=False,
        comment="Incremented on each update for conflict detection"
    )
    
    sync_status: Mapped[str] = mapped_column(
        String(20),
        default='synced',
        nullable=False,
        index=True,
        comment="synced, pending, conflict, deleted"
    )
    
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful sync with server"
    )
    
    sync_hash: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA256 hash for detecting changes"
    )
    
    def mark_as_pending_sync(self):
        """Mark record as pending sync"""
        self.sync_status = 'pending'
        # Initialize sync_version to 1 if it's None (for new objects)
        if self.sync_version is None:
            self.sync_version = 1
        else:
            self.sync_version += 1
    
    def mark_as_synced(self):
        """Mark record as successfully synced"""
        self.sync_status = 'synced'
        self.last_synced_at = datetime.now(timezone.utc)
    
    def mark_as_conflict(self):
        """Mark record as having sync conflict"""
        self.sync_status = 'conflict'


@declarative_mixin
class SoftDeleteMixin:
    """Mixin for soft delete functionality"""
    
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )
    
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True
    )