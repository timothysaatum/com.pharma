"""add version_vector columns and unresolved_conflicts table

Revision ID: b2c3d4e5f6a7
Revises: f2a3b4c5d6e7
Create Date: 2026-08-13

Phase 3 — LWW + vector clocks.

Adds a `version_vector` JSONB column (default empty object) to the three
reference-data tables that are written from multiple branches offline:
customers, drugs, drug_categories.

Creates the `unresolved_conflicts` table where concurrent edits are parked
for manual resolution by a manager.
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa

from app.models.db_types import JSONB, UUID

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── version_vector columns ────────────────────────────────────────────
    for table in ("customers", "drugs", "drug_categories"):
        op.add_column(
            table,
            sa.Column(
                "version_vector",
                JSONB(),
                nullable=False,
                server_default="{}",
            ),
        )

    # ── unresolved_conflicts ──────────────────────────────────────────────
    op.create_table(
        "unresolved_conflicts",
        sa.Column(
            "id",
            UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", UUID(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", UUID(), nullable=False),
        # FK to event_log via composite PK (org_id, event_id); nullable so
        # server-side conflicts with no originating client event can still be recorded.
        sa.Column("event_id", sa.Text(), nullable=True),
        sa.Column(
            "local_vector",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "local_snapshot",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "incoming_vector",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "incoming_payload",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved_keep_local','resolved_keep_incoming')",
            name="ck_unresolved_conflicts_status",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["event_log.org_id", "event_log.event_id"],
            ondelete="CASCADE",
            name="fk_unresolved_conflicts_event_id",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name="fk_unresolved_conflicts_org_id",
        ),
    )

    op.create_index(
        "ix_unresolved_conflicts_org_status",
        "unresolved_conflicts",
        ["org_id", "status", "created_at"],
    )
    op.create_index(
        "ix_unresolved_conflicts_aggregate",
        "unresolved_conflicts",
        ["aggregate_type", "aggregate_id"],
    )


def downgrade() -> None:
    op.drop_table("unresolved_conflicts")
    for table in ("customers", "drugs", "drug_categories"):
        op.drop_column(table, "version_vector")
