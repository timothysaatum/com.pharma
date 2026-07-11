"""repair audit log timestamp and sync columns

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-11 17:30:00.000000

The initial migration created ``audit_logs`` before ``AuditLog`` inherited
TimestampMixin and SyncTrackingMixin. Development ``create_all`` masked this
drift, but production sync pulls failed while selecting the missing columns.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d2e3f4a5b6c7"
down_revision: str = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("audit_logs")}


def _index_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes("audit_logs")}


def upgrade() -> None:
    columns = _column_names()
    additions = {
        "updated_at": sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        "sync_version": sa.Column(
            "sync_version", sa.BigInteger(), nullable=False,
            server_default=sa.text("1"),
        ),
        "sync_status": sa.Column(
            "sync_status", sa.String(length=20), nullable=False,
            server_default=sa.text("'synced'"),
        ),
        "last_synced_at": sa.Column(
            "last_synced_at", sa.DateTime(timezone=True), nullable=True,
        ),
        "sync_hash": sa.Column(
            "sync_hash", sa.String(length=64), nullable=True,
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("audit_logs", column)

    indexes = _index_names()
    if "ix_audit_logs_updated_at" not in indexes:
        op.create_index("ix_audit_logs_updated_at", "audit_logs", ["updated_at"])
    if "ix_audit_logs_sync_status" not in indexes:
        op.create_index("ix_audit_logs_sync_status", "audit_logs", ["sync_status"])


def downgrade() -> None:
    indexes = _index_names()
    for name in ("ix_audit_logs_sync_status", "ix_audit_logs_updated_at"):
        if name in indexes:
            op.drop_index(name, table_name="audit_logs")

    columns = _column_names()
    for name in (
        "sync_hash", "last_synced_at", "sync_status", "sync_version", "updated_at",
    ):
        if name in columns:
            op.drop_column("audit_logs", name)
