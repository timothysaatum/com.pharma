
from sqlalchemy import (
    String, Integer, Boolean, Numeric, Text, 
    ForeignKey, Index, CheckConstraint, event
)
from app.models.db_types import UUID
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship,
    validates
)
from app.db.base import Base
from typing import Optional, List, TYPE_CHECKING
import uuid

from app.models.core.mixins import SoftDeleteMixin, SyncTrackingMixin, TimestampMixin
from app.schemas.base_schemas import Money
if TYPE_CHECKING:
    from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
    from app.models.pharmacy.pharmacy_model import Organization
    

class DrugCategory(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """Hierarchical drug categories"""
    __tablename__ = 'drug_categories'
    
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
    description: Mapped[Optional[str]] = mapped_column(Text)
    
    # Hierarchical support
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drug_categories.id', ondelete='CASCADE'),
        nullable=True,
        index=True
    )
    
    # Materialized path for efficient queries
    path: Mapped[Optional[str]] = mapped_column(
        String(500),
        comment="Materialized path like /electronics/computers/laptops/"
    )
    
    level: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="Depth in hierarchy (0 = root)"
    )
    
    # Relationships
    parent: Mapped[Optional["DrugCategory"]] = relationship(
        remote_side=[id],
        back_populates="children"
    )
    children: Mapped[List["DrugCategory"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan"
    )
    drugs: Mapped[List["Drug"]] = relationship(back_populates="category")
    
    __table_args__ = (
        Index('idx_category_org', 'organization_id'),
        Index('idx_category_parent', 'parent_id'),
        Index('idx_category_path', 'path'),
    )


class Drug(Base, TimestampMixin, SyncTrackingMixin, SoftDeleteMixin):
    """
    Main drug/product catalog.
    Optimized for offline-first with comprehensive indexing.
    """
    __tablename__ = 'drugs'
    
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
    
    # Basic information
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Brand or trade name"
    )
    
    generic_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        index=True,
        comment="Generic/scientific name"
    )
    
    brand_name: Mapped[Optional[str]] = mapped_column(String(255))
    
    # Identifiers
    sku: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="Stock Keeping Unit"
    )
    
    barcode: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="EAN, UPC, or other barcode"
    )
    
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('drug_categories.id', ondelete='SET NULL'),
        nullable=True,
        index=True
    )
    
    # Drug classification
    drug_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        default='otc',
        comment="prescription, otc, controlled, herbal, supplement"
    )
    
    dosage_form: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="tablet, capsule, syrup, injection, cream, etc."
    )
    
    strength: Mapped[Optional[str]] = mapped_column(
        String(100),
        comment="e.g., 500mg, 10mg/ml"
    )
    
    # Regulatory information
    manufacturer: Mapped[Optional[str]] = mapped_column(String(255))
    supplier: Mapped[Optional[str]] = mapped_column(String(255))
    
    ndc_code: Mapped[Optional[str]] = mapped_column(
        String(50),
        comment="National Drug Code (US)"
    )
    
    requires_prescription: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )
    
    controlled_substance_schedule: Mapped[Optional[str]] = mapped_column(
        String(10),
        comment="DEA Schedule I-V for controlled substances"
    )
    
    # Pricing (stored as Decimal for precision)
    unit_price: Mapped[Money] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        index=True,
        comment="Selling price per unit"
    )
    
    cost_price: Mapped[Optional[Money]] = mapped_column(
        Numeric(10, 2),
        comment="Cost/acquisition price per unit"
    )
    
    markup_percentage: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2),
        comment="Profit margin percentage"
    )
    
    tax_rate: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0,
        nullable=False,
        comment="Tax rate as percentage"
    )
    
    # Stock management thresholds
    reorder_level: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
        comment="Trigger reorder alert when stock falls below"
    )
    
    reorder_quantity: Mapped[int] = mapped_column(
        Integer,
        default=50,
        nullable=False,
        comment="Suggested reorder quantity"
    )
    
    max_stock_level: Mapped[Optional[int]] = mapped_column(
        Integer,
        comment="Maximum stock to maintain"
    )
    
    unit_of_measure: Mapped[str] = mapped_column(
        String(50),
        default='unit',
        nullable=False,
        comment="unit, box, bottle, strip, etc."
    )
    
    # Additional information
    description: Mapped[Optional[str]] = mapped_column(Text)
    usage_instructions: Mapped[Optional[str]] = mapped_column(Text)
    side_effects: Mapped[Optional[str]] = mapped_column(Text)
    contraindications: Mapped[Optional[str]] = mapped_column(Text)
    storage_conditions: Mapped[Optional[str]] = mapped_column(Text)
    
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    
    # Active status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True
    )
    
    # Full-text search helper (tsvector for PostgreSQL)
    search_vector: Mapped[Optional[str]] = mapped_column(
        Text,
        comment="Generated tsvector for full-text search"
    )
    
    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="drugs")
    category: Mapped[Optional["DrugCategory"]] = relationship(back_populates="drugs")
    inventory: Mapped[List["BranchInventory"]] = relationship(
        back_populates="drug",
        cascade="all, delete-orphan"
    )
    batches: Mapped[List["DrugBatch"]] = relationship(
        back_populates="drug",
        cascade="all, delete-orphan"
    )
    
    __table_args__ = (
        CheckConstraint(
            "drug_type IN ('prescription', 'otc', 'controlled', 'herbal', 'supplement')",
            name='check_drug_type'
        ),
        CheckConstraint(
            "unit_price >= 0",
            name='check_unit_price_positive'
        ),
        CheckConstraint(
            "cost_price IS NULL OR cost_price >= 0",
            name='check_cost_price_positive'
        ),
        # Comprehensive indexes for fast queries
        Index('idx_drug_org', 'organization_id'),
        Index('uq_drug_org_sku', 'organization_id', 'sku', unique=True),
        Index('uq_drug_org_barcode', 'organization_id', 'barcode', unique=True),
        Index('idx_drug_name', 'name'),
        Index('idx_drug_generic', 'generic_name'),
        Index('idx_drug_sku', 'sku'),
        Index('idx_drug_barcode', 'barcode'),
        Index('idx_drug_category', 'category_id'),
        Index('idx_drug_type', 'drug_type'),
        Index('idx_drug_active', 'is_active'),
        Index('idx_drug_sync', 'sync_status', 'sync_version'),
        # Composite indexes for common queries
        Index('idx_drug_org_active', 'organization_id', 'is_active'),
        Index('idx_drug_org_type', 'organization_id', 'drug_type'),
        Index(
            'idx_drug_search',
            'search_vector',
            postgresql_using='gin',
            postgresql_ops={'search_vector': 'gin_trgm_ops'},
        ),
    )
    
    @validates('unit_price', 'cost_price')
    def validate_price(self, key, value):
        """Ensure prices are non-negative"""
        if value is not None and value < 0:
            raise ValueError(f"{key} must be non-negative")
        return value


# Trigger to update search_vector (PostgreSQL)
@event.listens_for(Drug, 'before_insert')
@event.listens_for(Drug, 'before_update')
def update_search_vector(mapper, connection, target):
    """Update full-text search vector"""
    search_parts = [
        target.name or '',
        target.generic_name or '',
        target.brand_name or '',
        target.sku or '',
        target.manufacturer or ''
    ]
    # This would use PostgreSQL's to_tsvector in production
    target.search_vector = ' '.join(filter(None, search_parts))
