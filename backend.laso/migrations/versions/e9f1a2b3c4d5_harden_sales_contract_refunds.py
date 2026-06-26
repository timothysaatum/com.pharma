"""harden sales contract controls and refunds

Revision ID: e9f1a2b3c4d5
Revises: c8d9e0f1a2b3
Create Date: 2026-06-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9f1a2b3c4d5'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('price_contracts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'requires_approval',
                sa.Boolean(),
                server_default=sa.text('FALSE'),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column('daily_usage_limit', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('per_customer_usage_limit', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'requires_preauthorization',
                sa.Boolean(),
                server_default=sa.text('FALSE'),
                nullable=False,
            )
        )
        batch_op.drop_constraint('check_contract_type', type_='check')
        batch_op.drop_constraint('check_discount_percentage_range', type_='check')
        batch_op.create_check_constraint(
            'check_contract_type',
            "contract_type IN ('insurance', 'corporate', 'staff', 'senior_citizen', 'standard', 'wholesale', 'promotional')",
        )
        batch_op.create_check_constraint(
            'check_discount_percentage_range',
            "discount_percentage >= 0 AND (discount_type != 'percentage' OR discount_percentage <= 100)",
        )
        batch_op.create_check_constraint(
            'check_contract_daily_usage_limit',
            'daily_usage_limit IS NULL OR daily_usage_limit > 0',
        )
        batch_op.create_check_constraint(
            'check_contract_customer_usage_limit',
            'per_customer_usage_limit IS NULL OR per_customer_usage_limit > 0',
        )

    with op.batch_alter_table('sale_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'refunded_quantity',
                sa.Integer(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            'check_sale_item_refunded_quantity',
            'refunded_quantity >= 0',
        )
        batch_op.create_check_constraint(
            'check_sale_item_refunded_not_exceed_quantity',
            'refunded_quantity <= quantity',
        )

    with op.batch_alter_table('sale_item_batch_allocations', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'refunded_quantity',
                sa.Integer(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            'check_sale_item_batch_alloc_refunded_qty',
            'refunded_quantity >= 0',
        )
        batch_op.create_check_constraint(
            'check_sale_item_batch_alloc_refunded_not_exceed',
            'refunded_quantity <= quantity',
        )


def downgrade() -> None:
    with op.batch_alter_table('sale_item_batch_allocations', schema=None) as batch_op:
        batch_op.drop_constraint('check_sale_item_batch_alloc_refunded_not_exceed', type_='check')
        batch_op.drop_constraint('check_sale_item_batch_alloc_refunded_qty', type_='check')
        batch_op.drop_column('refunded_quantity')

    with op.batch_alter_table('sale_items', schema=None) as batch_op:
        batch_op.drop_constraint('check_sale_item_refunded_not_exceed_quantity', type_='check')
        batch_op.drop_constraint('check_sale_item_refunded_quantity', type_='check')
        batch_op.drop_column('refunded_quantity')

    with op.batch_alter_table('price_contracts', schema=None) as batch_op:
        batch_op.drop_constraint('check_contract_customer_usage_limit', type_='check')
        batch_op.drop_constraint('check_contract_daily_usage_limit', type_='check')
        batch_op.drop_constraint('check_discount_percentage_range', type_='check')
        batch_op.drop_constraint('check_contract_type', type_='check')
        batch_op.create_check_constraint(
            'check_discount_percentage_range',
            'discount_percentage >= 0 AND discount_percentage <= 100',
        )
        batch_op.create_check_constraint(
            'check_contract_type',
            "contract_type IN ('insurance', 'corporate', 'staff', 'senior_citizen', 'standard', 'wholesale')",
        )
        batch_op.drop_column('requires_preauthorization')
        batch_op.drop_column('per_customer_usage_limit')
        batch_op.drop_column('daily_usage_limit')
        batch_op.drop_column('requires_approval')
