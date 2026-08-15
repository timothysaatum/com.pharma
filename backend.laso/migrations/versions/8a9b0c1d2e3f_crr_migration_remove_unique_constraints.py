"""crr migration: remove unique constraints, add renumber audit table

Removes UNIQUE constraints that block cr-sqlite CRDT operation from:

  - branch_inventory (uq_branch_drug)
  - drug_batches      (uq_branch_drug_batch)
  - sales             (uq_sale_branch_number)
  - purchase_orders   (uq_po_branch_number)
  - prescriptions     (uq_prescription_org_number)

Adds ``crr_renumber_audit`` table for tracking business-key renumbering
events in the ``keep_both_renumber`` merge strategy.

Revision ID: 8a9b0c1d2e3f
Revises: 2dac2bae15f3
Create Date: 2026-07-10 00:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a9b0c1d2e3f"
down_revision: Union[str, None] = "2dac2bae15f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_if_exists(name: str, table: str) -> None:
    """Drop a unique constraint (if it exists as a constraint) or index otherwise."""
    conn = op.get_bind()
    result = conn.exec_driver_sql(
        "SELECT contype FROM pg_constraint "
        "WHERE conname = %s AND conrelid = %s::regclass",
        (name, table),
    )
    row = result.fetchone() if result else None
    if row:
        conn.exec_driver_sql(
            f"ALTER TABLE {table} DROP CONSTRAINT {name}"
        )
    else:
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")


def upgrade() -> None:
    connector = op.get_context().bind.dialect.name

    if connector == "postgresql":
        _drop_if_exists("uq_branch_drug", "branch_inventory")
        _drop_if_exists("uq_branch_drug_batch", "drug_batches")
        _drop_if_exists("uq_sale_branch_number", "sales")
        _drop_if_exists("uq_po_branch_number", "purchase_orders")
        _drop_if_exists("uq_prescription_org_number", "prescriptions")

        op.create_table(
            "crr_renumber_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=512), nullable=False),
            sa.Column("table_name", sa.String(length=100), nullable=False),
            sa.Column("winner_id", sa.String(length=36), nullable=False),
            sa.Column("loser_id", sa.String(length=36), nullable=False),
            sa.Column("business_key_col", sa.String(length=100), nullable=False),
            sa.Column("old_business_key", sa.String(length=255), nullable=False),
            sa.Column("new_business_key", sa.String(length=255), nullable=False),
            sa.Column(
                "renumbered_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_crr_renumber_audit")),
            sa.UniqueConstraint("event_id", name="uq_crr_renumber_audit_event_id"),
        )
        op.create_table(
            "crr_customer_merge_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=255), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("survivor_id", sa.String(length=36), nullable=False),
            sa.Column("loser_id", sa.String(length=36), nullable=False),
            sa.Column("matched_fields", sa.JSON(), nullable=False),
            sa.Column("field_resolutions", sa.JSON(), nullable=False),
            sa.Column("merged_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_crr_customer_merge_audit")),
            sa.UniqueConstraint("event_id", name="uq_crr_customer_merge_audit_event_id"),
        )
    else:
        # SQLite — use batch mode
        try:
            with op.batch_alter_table("branch_inventory") as batch_op:
                batch_op.drop_constraint("uq_branch_drug", type_="unique")
        except Exception:
            pass
        try:
            with op.batch_alter_table("drug_batches") as batch_op:
                batch_op.drop_constraint("uq_branch_drug_batch", type_="unique")
        except Exception:
            pass
        try:
            with op.batch_alter_table("sales") as batch_op:
                batch_op.drop_constraint("uq_sale_branch_number", type_="unique")
        except Exception:
            pass
        try:
            with op.batch_alter_table("purchase_orders") as batch_op:
                batch_op.drop_constraint("uq_po_branch_number", type_="unique")
        except Exception:
            pass
        try:
            with op.batch_alter_table("prescriptions") as batch_op:
                batch_op.drop_constraint("uq_prescription_org_number", type_="unique")
        except Exception:
            pass

        op.create_table(
            "crr_renumber_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=512), nullable=False),
            sa.Column("table_name", sa.String(length=100), nullable=False),
            sa.Column("winner_id", sa.String(length=36), nullable=False),
            sa.Column("loser_id", sa.String(length=36), nullable=False),
            sa.Column("business_key_col", sa.String(length=100), nullable=False),
            sa.Column("old_business_key", sa.String(length=255), nullable=False),
            sa.Column("new_business_key", sa.String(length=255), nullable=False),
            sa.Column(
                "renumbered_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", name="uq_crr_renumber_audit_event_id"),
        )
        op.create_table(
            "crr_customer_merge_audit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=255), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("survivor_id", sa.String(length=36), nullable=False),
            sa.Column("loser_id", sa.String(length=36), nullable=False),
            sa.Column("matched_fields", sa.JSON(), nullable=False),
            sa.Column("field_resolutions", sa.JSON(), nullable=False),
            sa.Column("merged_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", name="uq_crr_customer_merge_audit_event_id"),
        )


def downgrade() -> None:
    connector = op.get_context().bind.dialect.name

    op.drop_table("crr_customer_merge_audit")
    op.drop_table("crr_renumber_audit")

    if connector == "postgresql":
        op.create_unique_constraint(
            "uq_branch_drug", "branch_inventory", ["branch_id", "drug_id"]
        )
        op.create_index(
            "uq_branch_drug_batch", "drug_batches",
            ["branch_id", "drug_id", "batch_number"], unique=True,
        )
        op.create_index(
            "uq_sale_branch_number", "sales",
            ["branch_id", "sale_number"], unique=True,
        )
        op.create_index(
            "uq_po_branch_number", "purchase_orders",
            ["branch_id", "po_number"], unique=True,
        )
        op.create_index(
            "uq_prescription_org_number", "prescriptions",
            ["organization_id", "prescription_number"], unique=True,
        )
    else:
        with op.batch_alter_table("branch_inventory") as batch_op:
            batch_op.create_unique_constraint(
                "uq_branch_drug", ["branch_id", "drug_id"]
            )
        with op.batch_alter_table("drug_batches") as batch_op:
            batch_op.create_unique_constraint(
                "uq_branch_drug_batch",
                ["branch_id", "drug_id", "batch_number"],
            )
        with op.batch_alter_table("sales") as batch_op:
            batch_op.create_unique_constraint(
                "uq_sale_branch_number", ["branch_id", "sale_number"]
            )
        with op.batch_alter_table("purchase_orders") as batch_op:
            batch_op.create_unique_constraint(
                "uq_po_branch_number", ["branch_id", "po_number"]
            )
        with op.batch_alter_table("prescriptions") as batch_op:
            batch_op.create_unique_constraint(
                "uq_prescription_org_number",
                ["organization_id", "prescription_number"],
            )
