"""repair onboarding idempotency schema drift

Revision ID: c3d4e5f6a7b8
Revises: 9b2c3d4e5f6a
Create Date: 2026-07-01 20:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "9b2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Restore the column on databases stamped past its original migration."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("organizations")
    }

    if "onboarding_idempotency_key" not in columns:
        op.add_column(
            "organizations",
            sa.Column(
                "onboarding_idempotency_key",
                sa.String(length=255),
                nullable=True,
                comment=(
                    "Stable key used to prevent duplicate organization "
                    "onboarding"
                ),
            ),
        )

    # Re-inspect because this migration may just have added the column.
    inspector = sa.inspect(bind)
    indexes = inspector.get_indexes("organizations")
    has_unique_index = any(
        index.get("unique")
        and index.get("column_names") == ["onboarding_idempotency_key"]
        for index in indexes
    )
    if not has_unique_index:
        op.create_index(
            "uq_organizations_onboarding_idempotency_key",
            "organizations",
            ["onboarding_idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    # This repair may be restoring a column owned by an older migration.
    # Dropping it here could destroy valid idempotency data.
    pass
