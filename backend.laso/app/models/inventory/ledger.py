from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.core.mixins import TimestampMixin
from app.models.db_types import JSONB, UUID


class InventoryMovement(Base, TimestampMixin):
    """
    Immutable inventory ledger row.

    BranchInventory and DrugBatch remain the fast read models, but this table is
    the durable explanation for every stock change.
    """

    __tablename__ = "inventory_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("drugs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("drug_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    movement_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment=(
            "purchase_receipt, sale, refund, damage, expired, theft, return, "
            "correction, transfer_out, transfer_in, batch_consume"
        ),
    )

    quantity_change: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Signed quantity delta. Positive increases stock, negative decreases stock.",
    )

    quantity_before: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)

    batch_quantity_before: Mapped[Optional[int]] = mapped_column(Integer)
    batch_quantity_after: Mapped[Optional[int]] = mapped_column(Integer)

    unit_cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))

    source_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        index=True,
        comment="sale, purchase_order, stock_adjustment, transfer, refund, batch",
    )
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Polymorphic ID of the source document.",
    )
    source_line_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Polymorphic ID of the source line/item.",
    )

    reference_number: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    context_metadata: Mapped[Optional[dict]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("quantity_after >= 0", name="check_movement_quantity_after"),
        CheckConstraint(
            "batch_quantity_before IS NULL OR batch_quantity_before >= 0",
            name="check_movement_batch_before",
        ),
        CheckConstraint(
            "batch_quantity_after IS NULL OR batch_quantity_after >= 0",
            name="check_movement_batch_after",
        ),
        CheckConstraint(
            "movement_type IN ("
            "'purchase_receipt', 'sale', 'refund', 'damage', 'expired', "
            "'theft', 'return', 'correction', 'transfer_out', 'transfer_in', "
            "'batch_consume'"
            ")",
            name="check_inventory_movement_type",
        ),
        Index("idx_movement_org_date", "organization_id", "occurred_at"),
        Index("idx_movement_branch_drug_date", "branch_id", "drug_id", "occurred_at"),
        Index("idx_movement_source", "source_type", "source_id"),
    )
