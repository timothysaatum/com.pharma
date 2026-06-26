"""add inventory ledger and sale batch allocations

Revision ID: c8d9e0f1a2b3
Revises: e7f8a9b0c1d2
Create Date: 2026-06-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app


revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'inventory_movements',
        sa.Column('id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('organization_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('branch_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('drug_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('batch_id', app.models.db_types.UUID(length=36), nullable=True),
        sa.Column('movement_type', sa.String(length=50), nullable=False),
        sa.Column('quantity_change', sa.Integer(), nullable=False),
        sa.Column('quantity_before', sa.Integer(), nullable=False),
        sa.Column('quantity_after', sa.Integer(), nullable=False),
        sa.Column('batch_quantity_before', sa.Integer(), nullable=True),
        sa.Column('batch_quantity_after', sa.Integer(), nullable=True),
        sa.Column('unit_cost', sa.Numeric(10, 2), nullable=True),
        sa.Column('unit_price', sa.Numeric(10, 2), nullable=True),
        sa.Column('source_type', sa.String(length=50), nullable=True),
        sa.Column('source_id', app.models.db_types.UUID(length=36), nullable=True),
        sa.Column('source_line_id', app.models.db_types.UUID(length=36), nullable=True),
        sa.Column('reference_number', sa.String(length=100), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_by', app.models.db_types.UUID(length=36), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('context_metadata', app.models.db_types.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint("quantity_after >= 0", name='check_movement_quantity_after'),
        sa.CheckConstraint(
            "batch_quantity_before IS NULL OR batch_quantity_before >= 0",
            name='check_movement_batch_before',
        ),
        sa.CheckConstraint(
            "batch_quantity_after IS NULL OR batch_quantity_after >= 0",
            name='check_movement_batch_after',
        ),
        sa.CheckConstraint(
            "movement_type IN ("
            "'purchase_receipt', 'sale', 'refund', 'damage', 'expired', "
            "'theft', 'return', 'correction', 'transfer_out', 'transfer_in', "
            "'batch_consume'"
            ")",
            name='check_inventory_movement_type',
        ),
        sa.ForeignKeyConstraint(['batch_id'], ['drug_batches.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['drug_id'], ['drugs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('inventory_movements', schema=None) as batch_op:
        batch_op.create_index('idx_movement_branch_drug_date', ['branch_id', 'drug_id', 'occurred_at'], unique=False)
        batch_op.create_index('idx_movement_org_date', ['organization_id', 'occurred_at'], unique=False)
        batch_op.create_index('idx_movement_source', ['source_type', 'source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_batch_id'), ['batch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_branch_id'), ['branch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_created_by'), ['created_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_drug_id'), ['drug_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_movement_type'), ['movement_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_occurred_at'), ['occurred_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_reference_number'), ['reference_number'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_source_id'), ['source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_source_line_id'), ['source_line_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_source_type'), ['source_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_inventory_movements_updated_at'), ['updated_at'], unique=False)

    op.create_table(
        'sale_item_batch_allocations',
        sa.Column('id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('sale_item_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('branch_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('drug_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('batch_id', app.models.db_types.UUID(length=36), nullable=True),
        sa.Column('batch_number', sa.String(length=100), nullable=True),
        sa.Column('batch_expiry_date', sa.Date(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_cost_at_sale', sa.Numeric(10, 2), nullable=True),
        sa.Column('unit_price_at_sale', sa.Numeric(10, 2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint("quantity > 0", name='check_sale_item_batch_alloc_qty'),
        sa.ForeignKeyConstraint(['batch_id'], ['drug_batches.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['drug_id'], ['drugs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['sale_item_id'], ['sale_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('sale_item_batch_allocations', schema=None) as batch_op:
        batch_op.create_index('idx_sale_item_batch_alloc_batch', ['batch_id'], unique=False)
        batch_op.create_index('idx_sale_item_batch_alloc_branch_drug', ['branch_id', 'drug_id'], unique=False)
        batch_op.create_index('idx_sale_item_batch_alloc_item', ['sale_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_batch_id'), ['batch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_branch_id'), ['branch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_drug_id'), ['drug_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_sale_item_id'), ['sale_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_item_batch_allocations_updated_at'), ['updated_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('sale_item_batch_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_updated_at'))
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_sale_item_id'))
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_drug_id'))
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_created_at'))
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_branch_id'))
        batch_op.drop_index(batch_op.f('ix_sale_item_batch_allocations_batch_id'))
        batch_op.drop_index('idx_sale_item_batch_alloc_item')
        batch_op.drop_index('idx_sale_item_batch_alloc_branch_drug')
        batch_op.drop_index('idx_sale_item_batch_alloc_batch')
    op.drop_table('sale_item_batch_allocations')

    with op.batch_alter_table('inventory_movements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_inventory_movements_updated_at'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_source_type'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_source_line_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_source_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_reference_number'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_organization_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_occurred_at'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_movement_type'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_drug_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_created_by'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_created_at'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_branch_id'))
        batch_op.drop_index(batch_op.f('ix_inventory_movements_batch_id'))
        batch_op.drop_index('idx_movement_source')
        batch_op.drop_index('idx_movement_org_date')
        batch_op.drop_index('idx_movement_branch_drug_date')
    op.drop_table('inventory_movements')
