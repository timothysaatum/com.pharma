"""repair role level schema drift

Revision ID: 1a2b3c4d5e6f
Revises: 0f1e2d3c4b5a
Create Date: 2026-07-01 16:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, None] = "0f1e2d3c4b5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add role hierarchy fields when their original migration was stamped."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("roles")}

    if "level" not in columns:
        op.add_column(
            "roles",
            sa.Column(
                "level",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
                comment=(
                    "Hierarchy level. Higher = more authority. Inherits "
                    "permissions from all lower levels."
                ),
            ),
        )

    indexes = {index["name"] for index in inspector.get_indexes("roles")}
    if "ix_roles_level" not in indexes:
        op.create_index(
            "ix_roles_level",
            "roles",
            ["level"],
            unique=False,
        )


def downgrade() -> None:
    # The original d5e6f7a8b9c0 migration owns this field. Do not remove valid
    # hierarchy data from databases where that original migration ran.
    pass
