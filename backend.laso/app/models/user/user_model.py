from app.db.base import Base
from sqlalchemy import (
    String, Integer, Boolean, DateTime, Text, 
    ForeignKey, Index, CheckConstraint
)
from app.models.db_types import UUID, ARRAY, INET, JSONB
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship,
    validates
)
from app.models.core.mixins import pwd_context
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
import uuid
from enum import Enum

from app.models.core.mixins import TimestampMixin, SyncTrackingMixin, SoftDeleteMixin
if TYPE_CHECKING:
    from app.models.pharmacy.pharmacy_model import Organization
    from app.models.system_md.sys_models import AuditLog


class Permission(str, Enum):
    """Fixed system permissions"""
    MANAGE_USERS = "manage_users"
    MANAGE_BRANCHES = "manage_branches"
    MANAGE_DRUGS = "manage_drugs"
    VIEW_DRUGS = "view_drugs"
    MANAGE_SUPPLIERS = "manage_suppliers"
    APPROVE_PURCHASE_ORDERS = "approve_purchase_orders"
    MANAGE_INVENTORY = "manage_inventory"
    VIEW_INVENTORY = "view_inventory"
    PROCESS_SALES = "process_sales"
    PROCESS_REFUNDS = "process_refunds"
    VIEW_REPORTS = "view_reports"
    EXPORT_DATA = "export_data"
    MANAGE_CUSTOMERS = "manage_customers"
    MANAGE_PRESCRIPTIONS = "manage_prescriptions"
    VIEW_AUDIT_LOGS = "view_audit_logs"
    MANAGE_ORGANIZATION = "manage_organization"
    MANAGE_PRICING = "manage_pricing"


class Role(Base, TimestampMixin, SyncTrackingMixin):
    """Organization-specific roles with associated permissions"""
    __tablename__ = 'roles'

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

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # List of permission strings
    permissions: Mapped[List[str]] = mapped_column(
        ARRAY(String(100)),
        default=list,
        nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="roles")
    users: Mapped[List["User"]] = relationship(
        secondary="user_roles",
        back_populates="roles"
    )

    __table_args__ = (
        Index('idx_role_org', 'organization_id'),
        # Ensure role name is unique within an organization
        Index('uq_role_org_name', 'organization_id', 'name', unique=True),
    )


class UserRole(Base, TimestampMixin):
    """Junction table for User and Role (Many-to-Many)"""
    __tablename__ = 'user_roles'

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='CASCADE'),
        primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('roles.id', ondelete='CASCADE'),
        primary_key=True
    )


class User(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """
    User accounts with role-based access control.
    Supports multi-branch assignment.
    """
    __tablename__ = 'users'
    
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
    
    username: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True
    )
    
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True
    )
    
    # Argon2 hashed password
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="Hardcoded super admin flag with absolute access"
    )
    
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    employee_id: Mapped[Optional[str]] = mapped_column(String(50))
    
    # Multi-branch assignment
    assigned_branches: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        default=list,
        comment="Branches this user can access"
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Security tracking
    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    
    account_locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="Account lock after failed attempts"
    )
    
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    
    # Two-factor authentication
    two_factor_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    two_factor_secret: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="Encrypted TOTP secret"
    )
    
    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="users")
    roles: Mapped[List["Role"]] = relationship(
        secondary="user_roles",
        back_populates="users"
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(back_populates="user")
    
    __table_args__ = (
        Index('idx_user_org', 'organization_id'),
        Index('idx_user_active', 'is_active'),
        Index('idx_user_super_admin', 'is_super_admin'),
    )
    
    @validates('email')
    def validate_email(self, key, email):
        """Validate email format"""
        import re
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, email):
            raise ValueError("Invalid email format")
        return email.lower()
    
    def set_password(self, password: str):
        """Hash and set password using Argon2"""
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        self.password_hash = pwd_context.hash(password)
        self.password_changed_at = datetime.now(timezone.utc)
    
    def verify_password(self, password: str) -> bool:
        """Verify password against hash"""
        return pwd_context.verify(password, self.password_hash)
    
    def has_permission(self, permission: str) -> bool:
        """Check if user has specific permission"""
        if self.is_super_admin:
            return True
        
        # Aggregate permissions from all assigned roles
        for role in self.roles:
            if permission in role.permissions or "*" in role.permissions:
                return True
        
        return False
    
    def has_branch_access(self, branch_id: uuid.UUID) -> bool:
        """Check if user has access to a branch (instance method)"""
        return str(branch_id) in {str(b) for b in (self.assigned_branches or [])}


class UserSession(Base, TimestampMixin):
    """Track active user sessions for security"""
    __tablename__ = 'user_sessions'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    # SHA256 hash of JWT token for revocation
    token_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True
    )
    
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        unique=True,
        index=True
    )
    
    # Security tracking
    ip_address: Mapped[Optional[str]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True
    )
    
    is_revoked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )
    
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions")
    
    __table_args__ = (
        Index('idx_session_user', 'user_id'),
        Index('idx_session_token', 'token_hash'),
        Index('idx_session_expires', 'expires_at'),
        Index('idx_session_active', 'is_revoked', 'expires_at'),
    )
