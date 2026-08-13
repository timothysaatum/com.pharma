"""add event_dead_letter quarantine table

Phase 1.10: poison-event quarantine. Events that exceed MAX_EVALUATION_ATTEMPTS
deferred evaluations, receive REJECTED_PERMANENT from a projector during drain,
or trigger repeated apply exceptions are moved here and removed from
pending_projections. The event_log row is preserved for audit; this table
records WHY projection was abandoned.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models.db_types import UUID


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_dead_letter",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("evaluation_attempts", sa.Integer(), nullable=False),
        # "max_attempts_deferred" | "rejected_permanent" | "apply_exception"
        sa.Column("quarantine_reason", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("first_deferred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "quarantined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("org_id", "event_id", name="pk_event_dead_letter"),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["event_log.org_id", "event_log.event_id"],
            ondelete="CASCADE",
            name="fk_event_dead_letter_event_log",
        ),
    )

    op.create_index(
        "ix_event_dead_letter_org",
        "event_dead_letter",
        ["org_id", "quarantined_at"],
    )

    op.execute("""
        COMMENT ON TABLE event_dead_letter IS
        'Poison-event quarantine per Phase 1.10. Rows here were removed from '
        'pending_projections after exceeding MAX_EVALUATION_ATTEMPTS, receiving '
        'REJECTED_PERMANENT during drain, or repeated apply exceptions. '
        'The event_log row is intact for audit. Operator action required.'
    """)


def downgrade() -> None:
    op.drop_index("ix_event_dead_letter_org", table_name="event_dead_letter")
    op.drop_table("event_dead_letter")
