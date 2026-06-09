from app.db.base import Base
from sqlalchemy import (
    String, Integer, DateTime, Text,
    ForeignKey, Index, CheckConstraint, Date
)
from app.models.db_types import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, date
import uuid

from app.models.core.mixins import SyncTrackingMixin, TimestampMixin
if TYPE_CHECKING:
    from app.models.customer.customer_model import Customer


class Prescription(Base, TimestampMixin, SyncTrackingMixin):
    """
    Track prescriptions for controlled substances and regulatory compliance.
    """
    __tablename__ = 'prescriptions'
    
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
    
    prescription_number: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True
    )
    
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('customers.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    
    # Prescriber information
    prescriber_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prescriber_license: Mapped[str] = mapped_column(String(100), nullable=False)
    prescriber_phone: Mapped[Optional[str]] = mapped_column(String(20))
    prescriber_address: Mapped[Optional[str]] = mapped_column(Text)
    
    # Prescription details
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    
    # Medications prescribed
    medications: Mapped[List[dict]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Array of { drug_id, drug_name, dosage, frequency, duration, quantity }"
    )
    
    # Diagnosis and notes
    diagnosis: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    special_instructions: Mapped[Optional[str]] = mapped_column(Text)
    
    # Refill information
    refills_allowed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    
    refills_remaining: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    
    last_refill_date: Mapped[Optional[date]] = mapped_column(Date)
    
    # Status
    status: Mapped[str] = mapped_column(
        String(50),
        default='active',
        nullable=False,
        index=True,
        comment="active, filled, expired, cancelled"
    )
    
    # Verification
    verified_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        comment="Pharmacist who verified prescription"
    )
    
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # Relationships
    customer: Mapped["Customer"] = relationship(back_populates="prescriptions")
    
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'filled', 'expired', 'cancelled')",
            name='check_prescription_status'
        ),
        CheckConstraint("refills_remaining >= 0", name='check_refills_remaining'),
        CheckConstraint("refills_remaining <= refills_allowed", name='check_refills_valid'),
        Index('idx_prescription_org', 'organization_id'),
        Index('idx_prescription_customer', 'customer_id'),
        Index('idx_prescription_issue_date', 'issue_date'),
        Index('idx_prescription_expiry', 'expiry_date'),
        Index('idx_prescription_status', 'status'),
    )