"""add password reset fields to users table

Revision ID: a1b2c3d4e5f6
Revises: 348ce2e1f2be
Create Date: 2026-06-23 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '348ce2e1f2be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'reset_token_hash',
                sa.String(length=255),
                nullable=True,
                comment='SHA256 hash of password reset token'
            )
        )
        batch_op.add_column(
            sa.Column(
                'reset_token_expires_at',
                sa.DateTime(timezone=True),
                nullable=True,
                comment='Expiration timestamp for reset token'
            )
        )
        batch_op.create_index(
            batch_op.f('ix_users_reset_token_hash'),
            ['reset_token_hash'],
            unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_reset_token_hash'))
        batch_op.drop_column('reset_token_expires_at')
        batch_op.drop_column('reset_token_hash')
