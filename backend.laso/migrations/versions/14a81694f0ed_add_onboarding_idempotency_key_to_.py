"""add_onboarding_idempotency_key_to_organizations

Revision ID: 14a81694f0ed
Revises: 7e8f9a0b1c2d
Create Date: 2026-06-27 09:41:36.105570

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '14a81694f0ed'
down_revision: Union[str, None] = '7e8f9a0b1c2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('onboarding_idempotency_key', sa.String(), nullable=True)
    )
    op.create_index(
        'ix_organizations_onboarding_idempotency_key',
        'organizations',
        ['onboarding_idempotency_key'],
        unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_organizations_onboarding_idempotency_key', table_name='organizations')
    op.drop_column('organizations', 'onboarding_idempotency_key')