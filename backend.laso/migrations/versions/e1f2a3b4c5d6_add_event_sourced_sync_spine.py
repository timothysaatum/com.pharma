"""add event-sourced sync spine (event_log, pending_projections, aggregate_snapshots)

Phase 1a of the offline-sync rewrite per ADRs 0006/0007/0008/0009 and
`docs/plans/offline-sync-rewrite-plan.md`. Creates the three server-side
tables the event-sourced spine needs. Layer-2 conflict tables
(`unresolved_conflicts`, `version_vector` columns) are deferred to
Phase 3.

The `event_log` table is created non-partitioned in this revision.
ADR 0009 commits to per-org monthly partitioning "from day one" but
partitioning is deferred until the first organization approaches the
250 GB early-warning threshold — the query patterns baked in here
(WHERE org_id = ? AND seq > ?) are partition-agnostic, so partitioning
can be introduced later as a schema migration without changing
projector or endpoint code.

Revision ID: e1f2a3b4c5d6
Revises: c4d5e6f7a8b9
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models.db_types import JSONB, UUID


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── event_log ─────────────────────────────────────────────────────────
    # Append-only canonical log per ADR 0007. Never UPDATE or DELETE rows
    # here — retention is governed by ADR 0009. PK is (org_id, event_id)
    # so idempotent inserts by (org_id, event_id) are a single index probe;
    # per-org monotonic `seq` is enforced separately.
    op.create_table(
        "event_log",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "dependencies",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authored_by", UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", UUID(as_uuid=True), nullable=False),
        sa.Column("hash_self", sa.Text(), nullable=False),
        sa.Column("hash_prev", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("org_id", "event_id", name="pk_event_log"),
        sa.UniqueConstraint("org_id", "seq", name="uq_event_log_org_seq"),
        sa.CheckConstraint(
            "char_length(event_id) = 26",
            name="ck_event_log_event_id_ulid_length",
        ),
        sa.CheckConstraint(
            "char_length(hash_self) = 64 AND char_length(hash_prev) = 64",
            name="ck_event_log_hash_hex_length",
        ),
    )

    # Pull cursor: WHERE org_id = ? AND seq > ? ORDER BY seq LIMIT N
    op.create_index(
        "ix_event_log_pull_cursor",
        "event_log",
        ["org_id", "seq"],
        postgresql_include=["aggregate_type", "event_type"],
    )

    # Aggregate-scoped projector replay: WHERE org_id = ? AND aggregate_id = ?
    # ORDER BY seq — used by snapshot-resume replay.
    op.create_index(
        "ix_event_log_aggregate_replay",
        "event_log",
        ["org_id", "aggregate_id", "seq"],
    )

    # Aggregate-type filter for typed replay (all sale events, etc.).
    op.create_index(
        "ix_event_log_type_seq",
        "event_log",
        ["org_id", "aggregate_type", "seq"],
    )

    op.execute("""
        COMMENT ON TABLE event_log IS
        'Append-only canonical event log per ADR 0006/0007. Never UPDATE or '
        'DELETE rows here — retention is governed by ADR 0009.'
    """)
    op.execute("""
        COMMENT ON COLUMN event_log.seq IS
        'Per-org monotonic sequence assigned at accept-time. Client pull cursor.'
    """)
    op.execute("""
        COMMENT ON COLUMN event_log.hash_prev IS
        'hash_self of the preceding event in this org''s log. All-zeros for the '
        'org''s very first event. See ADR 0007 for canonical-JSON hash rule.'
    """)

    # ── pending_projections ───────────────────────────────────────────────
    # Deferred-projection queue per ADR 0007. A row here means the event is
    # durable in event_log but its projector has not yet run because at
    # least one declared dependency has not been projected.
    op.create_table(
        "pending_projections",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column(
            "unresolved_deps",
            sa.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "first_deferred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "last_evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "evaluation_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "event_id", name="pk_pending_projections"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["event_log.org_id", "event_log.event_id"],
            ondelete="CASCADE",
            name="fk_pending_projections_event_log",
        ),
    )

    # GIN index on unresolved_deps supports the "which pending rows had
    # this newly-projected event_id in their deps?" scan efficiently.
    op.create_index(
        "ix_pending_projections_unresolved_gin",
        "pending_projections",
        ["unresolved_deps"],
        postgresql_using="gin",
    )

    op.create_index(
        "ix_pending_projections_org",
        "pending_projections",
        ["org_id"],
    )

    op.execute("""
        COMMENT ON TABLE pending_projections IS
        'Deferred-projection queue for events whose dependencies have not '
        'yet been projected. See ADR 0007.'
    """)

    # ── aggregate_snapshots ──────────────────────────────────────────────
    # Schema-only in Phase 1 per ADR 0009. Table is created so projectors
    # can be written snapshot-compatible from the start; no snapshotting
    # job runs until an org crosses the 12-month / 500-GB threshold.
    op.create_table(
        "aggregate_snapshots",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_seq", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("state", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "org_id",
            "aggregate_type",
            "aggregate_id",
            "snapshot_seq",
            name="pk_aggregate_snapshots",
        ),
    )

    op.create_index(
        "ix_aggregate_snapshots_latest",
        "aggregate_snapshots",
        ["org_id", "aggregate_type", "aggregate_id", sa.text("snapshot_seq DESC")],
    )

    op.execute("""
        COMMENT ON TABLE aggregate_snapshots IS
        'Projector state checkpoints per ADR 0009. Empty until snapshotting '
        'activates. Projectors must resume from these plus events with '
        'seq > snapshot_seq.'
    """)


def downgrade() -> None:
    op.drop_index("ix_aggregate_snapshots_latest", table_name="aggregate_snapshots")
    op.drop_table("aggregate_snapshots")

    op.drop_index("ix_pending_projections_org", table_name="pending_projections")
    op.drop_index("ix_pending_projections_unresolved_gin", table_name="pending_projections")
    op.drop_table("pending_projections")

    op.drop_index("ix_event_log_type_seq", table_name="event_log")
    op.drop_index("ix_event_log_aggregate_replay", table_name="event_log")
    op.drop_index("ix_event_log_pull_cursor", table_name="event_log")
    op.drop_table("event_log")
