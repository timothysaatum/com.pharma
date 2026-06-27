"""scope business identifiers by tenant

Revision ID: 7e8f9a0b1c2d
Revises: 6d7e8f9a0b1c
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e8f9a0b1c2d"
down_revision: Union[str, None] = "6d7e8f9a0b1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCOPED_INDEXES = (
    (
        "drugs",
        "ix_drugs_sku",
        "uq_drug_org_sku",
        ["organization_id", "sku"],
    ),
    (
        "drugs",
        "ix_drugs_barcode",
        "uq_drug_org_barcode",
        ["organization_id", "barcode"],
    ),
    (
        "prescriptions",
        "ix_prescriptions_prescription_number",
        "uq_prescription_org_number",
        ["organization_id", "prescription_number"],
    ),
    (
        "sales",
        "ix_sales_sale_number",
        "uq_sale_branch_number",
        ["branch_id", "sale_number"],
    ),
    (
        "purchase_orders",
        "ix_purchase_orders_po_number",
        "uq_po_branch_number",
        ["branch_id", "po_number"],
    ),
    (
        "insurance_providers",
        "ix_insurance_providers_code",
        "uq_insurance_org_code",
        ["organization_id", "code"],
    ),
)


def upgrade() -> None:
    for table, global_index, scoped_index, columns in SCOPED_INDEXES:
        op.drop_index(global_index, table_name=table)
        op.create_index(scoped_index, table, columns, unique=True)

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "onboarding_idempotency_key",
                sa.String(length=255),
                nullable=True,
                comment="Stable key used to prevent duplicate organization onboarding",
            )
        )
        batch_op.create_index(
            "uq_organizations_onboarding_idempotency_key",
            ["onboarding_idempotency_key"],
            unique=True,
        )
        batch_op.drop_constraint("check_subscription_tier", type_="check")
        batch_op.create_check_constraint(
            "check_subscription_tier",
            "subscription_tier IN ('trial', 'basic', 'professional', 'enterprise')",
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint("check_subscription_tier", type_="check")
        batch_op.create_check_constraint(
            "check_subscription_tier",
            "subscription_tier IN ('basic', 'professional', 'enterprise')",
        )
        batch_op.drop_index("uq_organizations_onboarding_idempotency_key")
        batch_op.drop_column("onboarding_idempotency_key")

    for table, global_index, scoped_index, columns in reversed(SCOPED_INDEXES):
        op.drop_index(scoped_index, table_name=table)
        op.create_index(global_index, table, [columns[-1]], unique=True)
