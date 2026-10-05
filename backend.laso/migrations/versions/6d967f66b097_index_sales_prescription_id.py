"""index sales.prescription_id for the derived dispense count

Revision ID: 6d967f66b097
Revises: a8b9c0d1e2f3
Create Date: 2026-10-04 00:00:00.000000

The Prescriptions list is about to show "dispensed N", derived from
`count(sales WHERE prescription_id = ?)`. That is a per-row aggregate over the
single busiest table in the database, and `sales.prescription_id` had no index at
all — so the list would have been an N+1 full scan of `sales`.

Counting per page rather than per row is what keeps it to one query; the index is
what keeps that one query cheap. Both are in this change because either alone
leaves a full scan.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "6d967f66b097"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Partial: only rows that actually carry a prescription are ever counted, and
    # on a busy branch most sales are walk-in cash sales with no Rx at all.
    op.create_index(
        "ix_sales_prescription_id",
        "sales",
        ["prescription_id"],
        unique=False,
        postgresql_where=sa.text("prescription_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sales_prescription_id", table_name="sales")
