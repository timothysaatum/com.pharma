"""merge branch heads f6e5d4c3b2a1 and 5c824f626a8e

Revision ID: e7f8a9b0c1d2
Revises: f6e5d4c3b2a1, 5c824f626a8e
Create Date: 2026-06-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = ('f6e5d4c3b2a1', '5c824f626a8e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
