"""add failure_count to sync_operation_receipts and entity_type/entity_id
to system_alerts, for proactive stuck-sale-sync alerting (P1-8)

Revision ID: c4d5e6f7a8b9
Revises: 981560e39ae6
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "981560e39ae6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SyncOperationReceipt.failure_count: durable per-operation_id counter of
    # how many push attempts have resolved to result_kind='failed'. Backfills
    # to 0 for every existing receipt (server_default), which is correct --
    # a receipt currently sitting at result_kind='failed' had its failure
    # observed under the old code that didn't count it, so re-pushing it
    # resumes counting from 0 rather than guessing a historical count.
    op.add_column(
        "sync_operation_receipts",
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # SystemAlert.entity_type/entity_id: generic entity reference mirroring
    # AuditLog.entity_type/entity_id, used for precise dedup lookups (e.g.
    # "is there already an unresolved alert for entity_type='sale' AND
    # entity_id=<local_id>?") instead of string-matching alert titles.
    op.add_column(
        "system_alerts",
        sa.Column("entity_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "system_alerts",
        sa.Column(
            "entity_id",
            app.models.db_types.UUID(length=36),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_alert_entity",
        "system_alerts",
        ["entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_alert_entity", table_name="system_alerts")
    op.drop_column("system_alerts", "entity_id")
    op.drop_column("system_alerts", "entity_type")
    op.drop_column("sync_operation_receipts", "failure_count")
