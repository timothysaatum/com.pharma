"""align audit log sync column comments

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-11 17:35:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: str = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COMMENTS = {
    "sync_version": "Incremented on each update for conflict detection",
    "sync_status": "synced, pending, conflict, deleted",
    "last_synced_at": "Last successful sync with server",
    "sync_hash": "SHA256 hash for detecting changes",
}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for column, comment in COMMENTS.items():
        op.alter_column("audit_logs", column, comment=comment)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for column in COMMENTS:
        op.alter_column("audit_logs", column, comment=None)
