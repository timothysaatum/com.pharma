"""merge schema repair heads

Revision ID: 9b2c3d4e5f6a
Revises: 1a2b3c4d5e6f, 8f1c2d3e4a5b
Create Date: 2026-07-01 18:45:00.000000

"""
from typing import Sequence, Union


revision: str = "9b2c3d4e5f6a"
down_revision: Union[str, None] = ("1a2b3c4d5e6f", "8f1c2d3e4a5b")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
