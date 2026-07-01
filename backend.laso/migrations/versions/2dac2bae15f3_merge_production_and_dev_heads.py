"""merge production and dev heads

Revision ID: 2dac2bae15f3
Revises: b5d3e2f1a4c6, ed7c5d56c183
Create Date: 2026-07-01 14:13:56.193254

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2dac2bae15f3'
down_revision: Union[str, None] = ('b5d3e2f1a4c6', 'ed7c5d56c183')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
