"""add role level for hierarchical permission inheritance

Revision ID: d5e6f7a8b9c0
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23 07:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('roles', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'level',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
                comment='Hierarchy level. Higher = more authority. Inherits permissions from all lower levels.'
            )
        )
        batch_op.create_index(
            batch_op.f('ix_roles_level'),
            ['level'],
            unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('roles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_roles_level'))
        batch_op.drop_column('level')
