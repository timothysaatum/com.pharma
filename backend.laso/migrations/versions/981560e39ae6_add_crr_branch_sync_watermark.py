"""add crr_branch_sync_watermark for ack-bounded CRDT log compaction (P1-7)

Revision ID: 981560e39ae6
Revises: bc23de45fa67
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app


revision: str = "981560e39ae6"
down_revision: Union[str, None] = "bc23de45fa67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crr_branch_sync_watermark",
        sa.Column(
            "branch_id",
            app.models.db_types.UUID(length=36),
            nullable=False,
        ),
        sa.Column(
            "last_acked_db_version",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("branch_id"),
    )

    # crr_renumber_audit was created by 8a9b0c1d2e3f. It carries no natural
    # sync-derived retention signal of its own (it is never pulled by any
    # client), so ack-bounded compaction stamps it at write time with the
    # shadow DB's global db_version -- see ShadowDB._log_renumbering /
    # get_row_db_version. shadow_db.py's _ensure_audit_table also applies
    # this idempotently at runtime (covers environments that provision
    # their schema via Base.metadata.create_all instead of Alembic), but a
    # real migration is the correct way to evolve an already-Alembic-managed
    # table.
    op.add_column(
        "crr_renumber_audit",
        sa.Column("db_version_at_creation", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crr_renumber_audit", "db_version_at_creation")
    op.drop_table("crr_branch_sync_watermark")
