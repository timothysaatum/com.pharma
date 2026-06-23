"""add split_payment_details, insurance_preauth_number, contract_type to sales

Revision ID: f6e5d4c3b2a1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-23 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app


revision: str = 'f6e5d4c3b2a1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'split_payment_details',
                app.models.db_types.JSONB(),
                nullable=True,
                comment='Split payment breakdown: {method: amount}'
            )
        )
        batch_op.add_column(
            sa.Column(
                'insurance_preauth_number',
                sa.String(length=100),
                nullable=True,
                comment='Insurance pre-authorization number'
            )
        )
        batch_op.add_column(
            sa.Column(
                'contract_type',
                sa.String(length=50),
                nullable=True,
                comment='Snapshot of contract type (insurance, corporate, staff, etc.)'
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.drop_column('contract_type')
        batch_op.drop_column('insurance_preauth_number')
        batch_op.drop_column('split_payment_details')
