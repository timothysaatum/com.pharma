from app.db.base import Base
from sqlalchemy import String, Integer, DateTime, ForeignKey, Index, CheckConstraint
from app.models.db_types import UUID
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
import uuid

from app.models.core.mixins import TimestampMixin

class StockLease(Base, TimestampMixin):
    """
    Lease for local offline selling for a specific terminal.
    Prevents offline multi-terminal overselling.
    """
    __tablename__ = 'stock_leases'
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('branches.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drugs.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )
    
    terminal_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Client device/terminal ID"
    )
    
    leased_quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Quantity leased to this terminal"
    )
    
    consumed_quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Quantity already consumed from this lease"
    )
    
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True
    )
    
    status: Mapped[str] = mapped_column(
        String(20),
        default='active',
        nullable=False,
        index=True,
        comment="'active', 'expired', 'released'"
    )

    __table_args__ = (
        CheckConstraint("leased_quantity >= 0", name='check_leased_quantity_nonnegative'),
        CheckConstraint("consumed_quantity >= 0", name='check_consumed_quantity_nonnegative'),
        CheckConstraint("consumed_quantity <= leased_quantity", name='check_consumed_lte_leased'),
        CheckConstraint("status IN ('active', 'expired', 'released')", name='check_lease_status'),
        Index('idx_lease_branch_terminal', 'branch_id', 'terminal_id'),
    )
