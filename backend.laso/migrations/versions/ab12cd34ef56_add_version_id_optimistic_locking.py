"""Add version_id optimistic locking to branch_inventory and drug_batches

Revision ID: ab12cd34ef56
Revises: f0a1b2c3d4e5
Create Date: 2026-07-21 01:00:00.000000

Adds an explicit version_id INTEGER column (default 1, not null) to
branch_inventory and drug_batches as a dedicated optimistic-lock counter.
The sync service increments this column on every inventory deduction so a
concurrent writer that reads a stale version_id will detect the race.

Note: SyncTrackingMixin already provides sync_version for internal sync
state tracking. version_id is a separate, application-level optimistic lock
counter that is never reset by sync status changes and is purely for
concurrency detection.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ab12cd34ef56"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ── branch_inventory ────────────────────────────────────────────────────────
    if "version_id" not in _column_names(bind, "branch_inventory"):
        with op.batch_alter_table("branch_inventory") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "version_id",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                    comment=(
                        "Optimistic lock counter. Incremented on every inventory "
                        "deduction. A concurrent writer that reads a stale version_id "
                        "will detect the race and retry with fresh data."
                    ),
                )
            )

    # ── drug_batches ─────────────────────────────────────────────────────────────
    if "version_id" not in _column_names(bind, "drug_batches"):
        with op.batch_alter_table("drug_batches") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "version_id",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                    comment=(
                        "Optimistic lock counter. Incremented on every batch "
                        "deduction during offline sale sync."
                    ),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()

    if "version_id" in _column_names(bind, "branch_inventory"):
        with op.batch_alter_table("branch_inventory") as batch_op:
            batch_op.drop_column("version_id")

    if "version_id" in _column_names(bind, "drug_batches"):
        with op.batch_alter_table("drug_batches") as batch_op:
            batch_op.drop_column("version_id")
