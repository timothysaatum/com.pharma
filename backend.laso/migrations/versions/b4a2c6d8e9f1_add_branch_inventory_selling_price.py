"""add branch inventory selling price

Revision ID: b4a2c6d8e9f1
Revises: 9f3d2b1a7c4e
Create Date: 2026-05-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4a2c6d8e9f1'
down_revision: Union[str, None] = '9f3d2b1a7c4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('branch_inventory', schema=None) as batch_op:
        batch_op.add_column(sa.Column('selling_price', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('branch_inventory', schema=None) as batch_op:
        batch_op.drop_column('selling_price')
