"""fix schema drift

Revision ID: 7c05258c4b0e
Revises: 3c389bb7a980
Create Date: 2026-06-23 11:44:36.704454

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c05258c4b0e'
down_revision: Union[str, None] = '3c389bb7a980'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
