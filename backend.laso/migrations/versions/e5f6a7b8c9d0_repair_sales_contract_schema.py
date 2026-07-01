"""repair sales contract and refund schema drift

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-01 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _check_constraints(inspector: sa.Inspector, table: str) -> dict[str, str]:
    return {
        constraint["name"]: constraint.get("sqltext") or ""
        for constraint in inspector.get_check_constraints(table)
        if constraint.get("name")
    }


def _repair_price_contracts(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("price_contracts")
    }
    checks = _check_constraints(inspector, "price_contracts")

    with op.batch_alter_table("price_contracts") as batch_op:
        if "requires_approval" not in columns:
            batch_op.add_column(
                sa.Column(
                    "requires_approval",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )
        if "daily_usage_limit" not in columns:
            batch_op.add_column(
                sa.Column("daily_usage_limit", sa.Integer(), nullable=True)
            )
        if "per_customer_usage_limit" not in columns:
            batch_op.add_column(
                sa.Column(
                    "per_customer_usage_limit",
                    sa.Integer(),
                    nullable=True,
                )
            )
        if "requires_preauthorization" not in columns:
            batch_op.add_column(
                sa.Column(
                    "requires_preauthorization",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )

        contract_type_check = checks.get("check_contract_type", "").lower()
        if "promotional" not in contract_type_check:
            if "check_contract_type" in checks:
                batch_op.drop_constraint(
                    "check_contract_type",
                    type_="check",
                )
            batch_op.create_check_constraint(
                "check_contract_type",
                (
                    "contract_type IN ('insurance', 'corporate', 'staff', "
                    "'senior_citizen', 'standard', 'wholesale', "
                    "'promotional')"
                ),
            )

        discount_check = checks.get(
            "check_discount_percentage_range",
            "",
        ).lower()
        if "discount_type" not in discount_check:
            if "check_discount_percentage_range" in checks:
                batch_op.drop_constraint(
                    "check_discount_percentage_range",
                    type_="check",
                )
            batch_op.create_check_constraint(
                "check_discount_percentage_range",
                (
                    "discount_percentage >= 0 AND "
                    "(discount_type != 'percentage' OR "
                    "discount_percentage <= 100)"
                ),
            )

        if "check_contract_daily_usage_limit" not in checks:
            batch_op.create_check_constraint(
                "check_contract_daily_usage_limit",
                "daily_usage_limit IS NULL OR daily_usage_limit > 0",
            )
        if "check_contract_customer_usage_limit" not in checks:
            batch_op.create_check_constraint(
                "check_contract_customer_usage_limit",
                (
                    "per_customer_usage_limit IS NULL OR "
                    "per_customer_usage_limit > 0"
                ),
            )


def _repair_refunded_quantity(
    bind: sa.Connection,
    table: str,
    nonnegative_constraint: str,
    quantity_constraint: str,
) -> None:
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table)}
    checks = _check_constraints(inspector, table)

    with op.batch_alter_table(table) as batch_op:
        if "refunded_quantity" not in columns:
            batch_op.add_column(
                sa.Column(
                    "refunded_quantity",
                    sa.Integer(),
                    server_default=sa.text("0"),
                    nullable=False,
                )
            )
        if nonnegative_constraint not in checks:
            batch_op.create_check_constraint(
                nonnegative_constraint,
                "refunded_quantity >= 0",
            )
        if quantity_constraint not in checks:
            batch_op.create_check_constraint(
                quantity_constraint,
                "refunded_quantity <= quantity",
            )


def upgrade() -> None:
    """Restore every field owned by the skipped hardening migration."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "price_contracts" in tables:
        _repair_price_contracts(bind)
    if "sale_items" in tables:
        _repair_refunded_quantity(
            bind,
            "sale_items",
            "check_sale_item_refunded_quantity",
            "check_sale_item_refunded_not_exceed_quantity",
        )
    if "sale_item_batch_allocations" in tables:
        _repair_refunded_quantity(
            bind,
            "sale_item_batch_allocations",
            "check_sale_item_batch_alloc_refunded_qty",
            "check_sale_item_batch_alloc_refunded_not_exceed",
        )


def downgrade() -> None:
    # The original e9f1a2b3c4d5 migration owns these fields. This repair cannot
    # know which objects it created, so removing them could destroy valid data.
    pass
