from app.db.base import Base
from sqlalchemy import (
    String, Integer, Numeric, Text, 
    ForeignKey, Index, CheckConstraint, Date, text
)
from app.models.db_types import UUID
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship
)
from typing import Optional, TYPE_CHECKING
from datetime import date
import uuid


from app.models.core.mixins import SyncTrackingMixin, TimestampMixin
if TYPE_CHECKING:
    from app.models.pharmacy.pharmacy_model import Branch
    from app.models.inventory.inventory_model import Drug

class BranchInventory(Base, TimestampMixin, SyncTrackingMixin):
    """
    Current inventory levels per branch.
    Optimized for fast queries and offline sync.
    """
    __tablename__ = 'branch_inventory'
    
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
    
    # Quantity tracking
    quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Total available quantity"
    )
    
    reserved_quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Reserved for pending orders/prescriptions"
    )
    
    # Computed column for available quantity
    # available_quantity = quantity - reserved_quantity
    
    # Location tracking
    location: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="Shelf/bin location in warehouse"
    )

    selling_price: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
        comment="Branch-specific selling price override"
    )

    version_id: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        server_default="1",
        comment=(
            "Optimistic lock counter. Incremented on every inventory "
            "deduction so concurrent sync writers can detect stale reads."
        )
    )

    # Relationships
    branch: Mapped["Branch"] = relationship(back_populates="inventory")
    drug: Mapped["Drug"] = relationship(back_populates="inventory")
    
    __table_args__ = (
        # Deliberately no business-key UNIQUE constraint: CRR conflict
        # resolution must be able to retain concurrent offline rows.
        CheckConstraint("quantity >= 0", name='check_quantity_nonnegative'),
        CheckConstraint("reserved_quantity >= 0", name='check_reserved_nonnegative'),
        CheckConstraint("reserved_quantity <= quantity", name='check_reserved_lte_quantity'),
        CheckConstraint("selling_price IS NULL OR selling_price >= 0", name='check_selling_price_nonnegative'),
        Index('idx_inventory_branch', 'branch_id'),
        Index('idx_inventory_drug', 'drug_id'),
        Index('idx_inventory_sync', 'sync_status', 'sync_version'),
        Index('idx_inventory_quantity', 'quantity'),
    )


class DrugBatch(Base, TimestampMixin, SyncTrackingMixin):
    """
    Track drug batches for FIFO/FEFO and expiry management.
    Critical for regulatory compliance.
    """
    __tablename__ = 'drug_batches'
    
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
    
    batch_number: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Manufacturer's batch/lot number"
    )
    
    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Initial quantity received"
    )
    
    remaining_quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Current remaining quantity"
    )
    
    # Dates
    manufacturing_date: Mapped[Optional[date]] = mapped_column(Date)
    
    expiry_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        comment="Expiration date - critical for safety"
    )
    
    # Pricing for this batch
    cost_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    selling_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    
    version_id: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        server_default="1",
        comment=(
            "Optimistic lock counter. Incremented on every batch "
            "deduction during offline sale sync."
        )
    )

    # Supplier information
    supplier: Mapped[Optional[str]] = mapped_column(String(255))
    purchase_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('purchase_orders.id', ondelete='SET NULL'),
        nullable=True
    )

    # Relationships
    drug: Mapped["Drug"] = relationship(back_populates="batches")
    
    __table_args__ = (
        # Deliberately no business-key UNIQUE constraint; see CRR migration.
        CheckConstraint("remaining_quantity >= 0", name='check_batch_remaining'),
        CheckConstraint("remaining_quantity <= quantity", name='check_batch_remaining_lte_total'),
        Index('idx_batch_branch', 'branch_id'),
        Index('idx_batch_drug', 'drug_id'),
        Index('idx_batch_expiry', 'expiry_date'),
        Index('idx_batch_remaining', 'remaining_quantity'),
        Index('idx_batch_expiring_stock', 'expiry_date', 'remaining_quantity',
        postgresql_where=text('remaining_quantity > 0')),
    )


class StockAdjustment(Base, TimestampMixin):
    """
    Audit trail for inventory adjustments.
    Critical for accountability and fraud prevention.
    """
    __tablename__ = 'stock_adjustments'
    
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
    
    adjustment_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="damage, expired, theft, return, correction, transfer, purchase_receipt"
    )
    
    # Quantity change (negative for reductions)
    quantity_change: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Positive for additions, negative for reductions"
    )
    
    previous_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    new_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    
    reason: Mapped[Optional[str]] = mapped_column(Text)
    
    adjusted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    
    # For transfers
    transfer_to_branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('branches.id', ondelete='SET NULL')
    )
    
    __table_args__ = (
        CheckConstraint(
            "adjustment_type IN ('damage', 'expired', 'theft', 'return', 'correction', 'transfer', 'purchase_receipt')",
            name='check_adjustment_type'
        ),
        CheckConstraint("new_quantity >= 0", name='check_new_quantity'),
        Index('idx_adjustment_branch', 'branch_id'),
        Index('idx_adjustment_drug', 'drug_id'),
        Index('idx_adjustment_type', 'adjustment_type'),
        Index('idx_adjustment_date', 'created_at'),
    )
