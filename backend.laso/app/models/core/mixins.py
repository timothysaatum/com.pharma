
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
from cryptography.fernet import Fernet
import os

from app.core.config import get_settings

# Reuse password hashing context from security module to avoid duplicate CryptContext
from app.core.security import pwd_context as _security_pwd_context
pwd_context = _security_pwd_context

# Encryption for sensitive data
# ENCRYPTION_KEY is loaded lazily to avoid ordering issues with Settings init
_cipher_suite: Fernet | None = None


def get_cipher_suite() -> Fernet:
    global _cipher_suite
    if _cipher_suite is not None:
        return _cipher_suite
    raw_key = os.environ.get("ENCRYPTION_KEY")
    if not raw_key:
        try:
            raw_key = get_settings().ENCRYPTION_KEY
        except Exception:
            pass
    if not raw_key:
        raise RuntimeError(
            "ENCRYPTION_KEY environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    key_bytes = raw_key.encode() if isinstance(raw_key, str) else raw_key
    _cipher_suite = Fernet(key_bytes)
    return _cipher_suite


cipher_suite = get_cipher_suite()


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