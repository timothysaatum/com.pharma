from app.db.base import Base
from sqlalchemy import (
    DateTime, String, Integer, Boolean, ForeignKey, Index, CheckConstraint, Date, Text, text
)
from app.models.db_types import UUID, ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional, List, TYPE_CHECKING
from datetime import date, datetime
import uuid
from app.models.core.mixins import SoftDeleteMixin, SoftDeleteMixin, SyncTrackingMixin, TimestampMixin
from app.models.pricing.pricing_model import InsuranceProvider, PriceContract

if TYPE_CHECKING:
    from app.models.precriptions.prescription_model import Prescription
    from app.models.sales.sales_model import Sale

class Customer(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """
    Customer information with privacy protection.
    Supports both walk-in and registered customers.
    """
    __tablename__ = 'customers'
    
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
    
    customer_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default='walk_in',
        index=True,
        comment="walk_in, registered"
    )
    
    # Personal information (optional for walk-in)
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    
    phone: Mapped[Optional[str]] = mapped_column(
        String(20),
        index=True
    )
    
    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        index=True
    )
    
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    
    # Address
    address: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="{ street, city, state, zip, country }"
    )
    
    # Health information (encrypted in production)
    # Should use application-level encryption
    allergies: Mapped[List[str]] = mapped_column(
        ARRAY(String),
        default=list,
        comment="Known drug allergies"
    )
    
    chronic_conditions: Mapped[List[str]] = mapped_column(
        ARRAY(String),
        default=list,
        comment="Chronic medical conditions"
    )
    
    # Medical record encryption flag
    medical_data_encrypted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="Whether PHI is encrypted"
    )
    
    # Loyalty program
    loyalty_points: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    
    loyalty_tier: Mapped[str] = mapped_column(
        String(50),
        default='bronze',
        comment="bronze, silver, gold, platinum"
    )

    # Denormalized purchase counters — kept in sync by SalesService
    total_orders: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Total number of completed sales for this customer"
    )

    total_value: Mapped[float] = mapped_column(
        default=0.0,
        nullable=False,
        comment="Cumulative spend across all completed sales (GHS)"
    )

    # Preferences
    preferred_contact_method: Mapped[str] = mapped_column(
        String(20),
        default='email',
        comment="email, phone, sms"
    )
    
    marketing_consent: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    
    # Customer status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )
    
    # Insurance information
    insurance_provider_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('insurance_providers.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        comment="Customer's insurance provider"
    )

    insurance_member_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        index=True,
        comment="Member/policy ID with insurance company"
    )

    insurance_card_image_url: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="Scanned insurance card for verification"
    )

    # Preferred price contract (can override insurance contract)
    preferred_contract_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('price_contracts.id', ondelete='SET NULL'),
        nullable=True,
        comment="Customer's preferred pricing contract (for corporate/staff)"
    )

    # Relationships
    insurance_provider: Mapped[Optional["InsuranceProvider"]] = relationship()
    preferred_contract: Mapped[Optional["PriceContract"]] = relationship()
    
    sales: Mapped[List["Sale"]] = relationship(back_populates="customer")
    prescriptions: Mapped[List["Prescription"]] = relationship(back_populates="customer")
    
    @property
    def has_insurance(self) -> bool:
        """Customer has insurance on file"""
        return self.insurance_provider_id is not None
    
    __table_args__ = (
        CheckConstraint(
            "customer_type IN ('walk_in', 'registered', 'insurance', 'corporate')",
            name='check_customer_type'
        ),
        CheckConstraint(
            "loyalty_tier IN ('bronze', 'silver', 'gold', 'platinum')",
            name='check_loyalty_tier'
        ),
        Index('idx_customer_org', 'organization_id'),
        Index('idx_customer_phone', 'phone'),
        Index('idx_customer_email', 'email'),
        Index('idx_customer_type', 'customer_type'),
        Index('idx_customer_loyalty', 'loyalty_points', 'loyalty_tier'),
    )