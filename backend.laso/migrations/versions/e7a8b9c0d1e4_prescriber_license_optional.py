"""make prescriptions.prescriber_license nullable

Revision ID: e7a8b9c0d1e4
Revises: 6d967f66b097
Create Date: 2026-10-04 00:00:00.000000

The prescriber's licence number is not always known when the prescription is
written, so it becomes optional (P2).

NO DATA BACKFILL, deliberately. The column has been NOT NULL since
`4a8c7a6b5ba3` and nothing has ever relaxed it, so no row can be NULL today.
An empty string IS reachable — `PrescriptionCreate.prescriber_license` had no
`min_length`, so a direct API caller could write `''` even though both UI forms
blocked it. Rather than rewrite history to a value nothing consumes, this is a
pure constraint change: reads already treat falsy as absent, and the next write
of a row normalises it to NULL.

Postgres only, matching `a8b9c0d1e2f3`. SQLite cannot DROP NOT NULL either, and
the device rebuilds the table in `migrate_v36`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7a8b9c0d1e4"
down_revision: Union[str, None] = "6d967f66b097"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.alter_column(
        "prescriptions",
        "prescriber_license",
        existing_type=sa.String(length=100),
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Any NULL written while the column was nullable would violate this, so fill
    # first rather than letting the constraint change fail on real data.
    op.execute(
        sa.text(
            "UPDATE prescriptions SET prescriber_license = '' "
            "WHERE prescriber_license IS NULL"
        )
    )
    op.alter_column(
        "prescriptions",
        "prescriber_license",
        existing_type=sa.String(length=100),
        nullable=False,
    )
