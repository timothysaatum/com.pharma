"""add must_change_password to users

Revision ID: b5d3e2f1a4c6
Revises: 14a81694f0ed
Create Date: 2026-06-27 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "b5d3e2f1a4c6"
down_revision: Union[str, None] = "14a81694f0ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="Newly created users must change password on first login",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
