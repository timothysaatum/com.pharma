"""repair password reset schema drift

Revision ID: 0f1e2d3c4b5a
Revises: 2dac2bae15f3
Create Date: 2026-07-01 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0f1e2d3c4b5a"
down_revision: Union[str, None] = "2dac2bae15f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add reset fields when a database was stamped past their migration."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}

    if "reset_token_hash" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "reset_token_hash",
                sa.String(length=255),
                nullable=True,
                comment="SHA256 hash of password reset token",
            ),
        )

    if "reset_token_expires_at" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "reset_token_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="Expiration timestamp for reset token",
            ),
        )

    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_reset_token_hash" not in indexes:
        op.create_index(
            "ix_users_reset_token_hash",
            "users",
            ["reset_token_hash"],
            unique=False,
        )


def downgrade() -> None:
    # The original a1b2c3d4e5f6 migration owns these fields. A repair migration
    # cannot know whether it created them, so removing them here could destroy
    # valid password-reset data on a database whose schema was already correct.
    pass
