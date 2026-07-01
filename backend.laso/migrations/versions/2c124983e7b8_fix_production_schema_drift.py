"""fix production schema drift

Revision ID: 2c124983e7b8
Revises: 3c389bb7a980
Create Date: 2026-06-23 11:59:05.370523

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c124983e7b8'
down_revision: Union[str, None] = '3c389bb7a980'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
