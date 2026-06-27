from app.db.base import Base

from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey, Index, CheckConstraint, text
)
from app.models.db_types import UUID, JSONB
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship
)
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
import uuid

from app.models.core.mixins import SoftDeleteMixin, SyncTrackingMixin, TimestampMixin
if TYPE_CHECKING:
    from app.models.inventory.branch_inventory import BranchInventory
    from app.models.inventory.inventory_model import Drug
    from app.models.sales.sales_model import Sale
    from app.models.user.user_model import User, Role


class Organization(Base, TimestampMixin, SyncTrackingMixin):
    """
    Root entity for multi-tenancy.
    All data is scoped to an organization.
    """
    __tablename__ = 'organizations'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )                                                                                               
    
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )
    
    type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="over_the_counter, pharmacy, hospital_pharmacy, chain"
    )
    
    license_number: Mapped[Optional[str]] = mapped_column(
        String(100),
        unique=True,
        nullable=True
    )
    
    tax_id: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True
    )
    
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    
    # Address as JSON for flexibility
    address: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="{ street, city, state, zip, country }"
    )
    
    # Organization-specific settings
    settings: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        comment="Tax rates, business hours, preferences, etc."
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Subscription/billing info
    subscription_tier: Mapped[str] = mapped_column(
        String(50),
        default='basic',
        comment="basic, professional, enterprise"
    )
    
    subscription_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    onboarding_idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Stable key used to prevent duplicate organization onboarding",
    )
    
    # Relationships
    branches: Mapped[List["Branch"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan"
    )
    roles: Mapped[List["Role"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan"
    )
    users: Mapped[List["User"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan"
    )
    drugs: Mapped[List["Drug"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan"
    )
    
    __table_args__ = (
        Index(
            'uq_organizations_onboarding_idempotency_key',
            'onboarding_idempotency_key',
            unique=True,
        ),
        CheckConstraint(
            "type IN ('otc', 'pharmacy', 'hospital_pharmacy', 'chain')",
            name='check_org_type'
        ),
        CheckConstraint(
            "subscription_tier IN ('trial', 'basic', 'professional', 'enterprise')",
            name='check_subscription_tier'
        ),
        Index('idx_org_active', 'is_active'),
        Index('idx_org_subscription', 'subscription_tier', 'subscription_expires_at'),
    )


class Branch(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """Branch/location within an organization"""
    __tablename__ = 'branches'
    
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
    
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    
    code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Unique branch code within an organization for transactions"
    )
    
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    
    address: Mapped[Optional[dict]] = mapped_column(JSONB)
    
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Operating hours
    operating_hours: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="{ monday: { open: '09:00', close: '18:00' }, ... }"
    )
    
    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="branches")
    inventory: Mapped[List["BranchInventory"]] = relationship(
        back_populates="branch",
        cascade="all, delete-orphan"
    )
    sales: Mapped[List["Sale"]] = relationship(back_populates="branch")
    
    __table_args__ = (
        Index('idx_branch_org', 'organization_id'),
        Index('idx_branch_active', 'is_active'),
        Index('idx_branch_code', 'code'),
        Index(
            'uq_branch_org_code_active',
            'organization_id',
            'code',
            unique=True,
            sqlite_where=text('is_deleted = 0'),
            postgresql_where=text('is_deleted = false'),
        ),
    )
