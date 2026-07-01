"""merge heads

Revision ID: ed7c5d56c183
Revises: 2c124983e7b8, 5c824f626a8e
Create Date: 2026-06-23 17:36:13.231106

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed7c5d56c183'
down_revision: Union[str, None] = ('2c124983e7b8', '5c824f626a8e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
