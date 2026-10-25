"""add sales.contract_resolution

Revision ID: b7c8d9e0f1a2
Revises: e7a8b9c0d1e4
Create Date: 2026-10-25 00:00:00.000000

Records how a sale's price contract was resolved. Existing rows stay NULL and
are treated as legacy. New online sales use client or server_default; offline
sales use client, missing_offline, or invalid_client_snapshot.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "e7a8b9c0d1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sales", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "contract_resolution",
                sa.String(length=32),
                nullable=True,
                comment="client, server_default, missing_offline, invalid_client_snapshot",
            )
        )
        batch_op.create_check_constraint(
            "check_sale_contract_resolution",
            "contract_resolution IS NULL OR contract_resolution IN "
            "('client', 'server_default', 'missing_offline', 'invalid_client_snapshot')",
        )


def downgrade() -> None:
    with op.batch_alter_table("sales", schema=None) as batch_op:
        batch_op.drop_constraint("check_sale_contract_resolution", type_="check")
        batch_op.drop_column("contract_resolution")
