"""add durable sync operation receipts

Revision ID: 6d7e8f9a0b1c
Revises: a91cfcc34459
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app


revision: str = "6d7e8f9a0b1c"
down_revision: Union[str, None] = "a91cfcc34459"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_operation_receipts",
        sa.Column(
            "operation_id",
            app.models.db_types.UUID(length=36),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            app.models.db_types.UUID(length=36),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            app.models.db_types.UUID(length=36),
            nullable=False,
        ),
        sa.Column("table_name", sa.String(length=100), nullable=False),
        sa.Column("record_id", sa.String(length=100), nullable=False),
        sa.Column(
            "result_kind",
            sa.String(length=20),
            nullable=False,
            comment="accepted, conflict, or failed",
        ),
        sa.Column(
            "response_data",
            app.models.db_types.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result_kind IN ('accepted', 'conflict', 'failed')",
            name="check_sync_receipt_result_kind",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    with op.batch_alter_table("sync_operation_receipts") as batch_op:
        batch_op.create_index(
            "idx_sync_receipt_scope_created",
            ["organization_id", "branch_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_operation_receipts_branch_id"),
            ["branch_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_operation_receipts_created_at"),
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_operation_receipts_organization_id"),
            ["organization_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("sync_operation_receipts")
