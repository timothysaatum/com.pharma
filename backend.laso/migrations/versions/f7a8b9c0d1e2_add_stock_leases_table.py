"""add stock leases table

Revision ID: f7a8b9c0d1e2
Revises: b2c3d4e5f6a7
Create Date: 2026-08-15 05:23:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS stock_leases (
            id UUID PRIMARY KEY,
            branch_id UUID NOT NULL,
            drug_id UUID NOT NULL,
            terminal_id VARCHAR(100) NOT NULL,
            leased_quantity INTEGER NOT NULL DEFAULT 0,
            consumed_quantity INTEGER NOT NULL DEFAULT 0,
            expires_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS ix_stock_leases_branch_terminal_drug 
            ON stock_leases (branch_id, terminal_id, drug_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stock_leases;")
