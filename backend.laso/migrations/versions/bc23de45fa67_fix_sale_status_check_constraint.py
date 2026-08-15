"""reconcile check-constraint drift between models and migrations

Revision ID: bc23de45fa67
Revises: ab12cd34ef56
Create Date: 2026-08-05 04:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "bc23de45fa67"
down_revision: Union[str, None] = "ab12cd34ef56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Found by diffing every CheckConstraint declared in app/models/ against
# every one ever created by a migration: these exist in the current ORM
# models but were never actually added to the database by any migration.
# The worst of the four — check_sale_status — meant partial refunds were
# completely broken (a 409 check-constraint violation) in every environment
# provisioned via `alembic upgrade head`, i.e. every real deployment,
# invisible to the test suite because tests build schema from the current
# model via Base.metadata.create_all(), bypassing Alembic entirely.
# Live-discovered while orchestrating a fresh deployment end-to-end; see
# docs/reviews/2026-08-05-live-deployment-orchestration-report.md.
_OLD_SALE_STATUSES = "('draft', 'completed', 'cancelled', 'refunded')"
_NEW_SALE_STATUSES = "('draft', 'completed', 'cancelled', 'refunded', 'partially_refunded')"


def _existing_check_constraints(bind: sa.Connection, table: str) -> set[str]:
    return {
        c["name"] for c in sa.inspect(bind).get_check_constraints(table)
        if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    sales_checks = _existing_check_constraints(bind, "sales")
    if "check_sale_status" in sales_checks:
        op.drop_constraint("check_sale_status", "sales", type_="check")
    op.create_check_constraint(
        "check_sale_status", "sales", f"status IN {_NEW_SALE_STATUSES}"
    )

    sale_item_checks = _existing_check_constraints(bind, "sale_items")
    if "check_sale_item_refunded_quantity" not in sale_item_checks:
        op.create_check_constraint(
            "check_sale_item_refunded_quantity", "sale_items", "refunded_quantity >= 0"
        )
    if "check_sale_item_refunded_not_exceed_quantity" not in sale_item_checks:
        op.create_check_constraint(
            "check_sale_item_refunded_not_exceed_quantity", "sale_items",
            "refunded_quantity <= quantity",
        )

    branch_inventory_checks = _existing_check_constraints(bind, "branch_inventory")
    if "check_selling_price_nonnegative" not in branch_inventory_checks:
        op.create_check_constraint(
            "check_selling_price_nonnegative", "branch_inventory",
            "selling_price IS NULL OR selling_price >= 0",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if "check_selling_price_nonnegative" in _existing_check_constraints(bind, "branch_inventory"):
        op.drop_constraint("check_selling_price_nonnegative", "branch_inventory", type_="check")

    sale_item_checks = _existing_check_constraints(bind, "sale_items")
    if "check_sale_item_refunded_not_exceed_quantity" in sale_item_checks:
        op.drop_constraint("check_sale_item_refunded_not_exceed_quantity", "sale_items", type_="check")
    if "check_sale_item_refunded_quantity" in sale_item_checks:
        op.drop_constraint("check_sale_item_refunded_quantity", "sale_items", type_="check")

    if "check_sale_status" in _existing_check_constraints(bind, "sales"):
        op.drop_constraint("check_sale_status", "sales", type_="check")
    op.create_check_constraint(
        "check_sale_status", "sales", f"status IN {_OLD_SALE_STATUSES}"
    )
