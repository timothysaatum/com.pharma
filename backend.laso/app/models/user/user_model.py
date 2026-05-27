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


class UserRole(str, Enum):
    """User roles for role-based access control"""
    super_admin = "super_admin"
    admin = "admin"
    manager = "manager"
    pharmacist = "pharmacist"
    cashier = "cashier"
    viewer = "viewer"


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
    
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="super_admin, admin, manager, pharmacist, cashier, viewer"
    )
    
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    employee_id: Mapped[Optional[str]] = mapped_column(String(50))
    
    # Multi-branch assignment
    assigned_branches: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        default=list,
        comment="Branches this user can access"
    )
    
    # Custom permissions (in addition to role)
    permissions: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        comment="{ additional: ['perm1', 'perm2'], denied: ['perm3'] }"
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
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(back_populates="user")
    
    __table_args__ = (
        CheckConstraint(
            "role IN ('super_admin', 'admin', 'manager', 'pharmacist', 'cashier', 'viewer')",
            name='check_user_role'
        ),
        Index('idx_user_org', 'organization_id'),
        Index('idx_user_active', 'is_active'),
        Index('idx_user_role', 'role'),
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
        # Check denied permissions first
        if permission in self.permissions.get('denied', []):
            return False
        
        # Check additional permissions
        if permission in self.permissions.get('additional', []):
            return True
        
        # Check role-based permissions
        role_permissions = {
            'super_admin': ['*'],
            'admin': ['manage_users', 'manage_branches', 'manage_drugs', 'manage_suppliers', 'approve_purchase_orders',
                     'manage_inventory', 'process_sales', 'view_reports', 'export_data', 'manage_customers'],
            'manager': ['manage_drugs', 'manage_inventory', 'process_sales', 'approve_purchase_orders',
                       'view_reports', 'export_data', 'manage_suppliers', 'manage_customers'],
            'pharmacist': ['view_drugs', 'process_sales', 'view_inventory', 'approve_purchase_orders',
                          'manage_prescriptions', 'manage_suppliers', 'manage_customers'],
            'cashier': ['view_drugs', 'process_sales', 'view_inventory', 'manage_customers'],
            'viewer': ['view_drugs', 'view_inventory', 'view_reports']
        }
        
        user_perms = role_permissions.get(self.role, [])
        return '*' in user_perms or permission in user_perms
    
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
