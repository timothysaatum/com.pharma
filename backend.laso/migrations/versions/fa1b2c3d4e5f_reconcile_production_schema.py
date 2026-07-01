"""reconcile remaining production schema drift

Revision ID: fa1b2c3d4e5f
Revises: e5f6a7b8c9d0
Create Date: 2026-07-01 21:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models.db_types import JSONB, UUID


revision: str = "fa1b2c3d4e5f"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCOPED_INDEXES = (
    ("drugs", "ix_drugs_sku", "uq_drug_org_sku", ("organization_id", "sku")),
    (
        "drugs",
        "ix_drugs_barcode",
        "uq_drug_org_barcode",
        ("organization_id", "barcode"),
    ),
    (
        "prescriptions",
        "ix_prescriptions_prescription_number",
        "uq_prescription_org_number",
        ("organization_id", "prescription_number"),
    ),
    (
        "sales",
        "ix_sales_sale_number",
        "uq_sale_branch_number",
        ("branch_id", "sale_number"),
    ),
    (
        "purchase_orders",
        "ix_purchase_orders_po_number",
        "uq_po_branch_number",
        ("branch_id", "po_number"),
    ),
    (
        "insurance_providers",
        "ix_insurance_providers_code",
        "uq_insurance_org_code",
        ("organization_id", "code"),
    ),
)


def _column_names(bind: sa.Connection, table: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(bind).get_columns(table)
    }


def _index_names(bind: sa.Connection, table: str) -> set[str]:
    return {
        index["name"] for index in sa.inspect(bind).get_indexes(table)
        if index.get("name")
    }


def _repair_prescription_branch(bind: sa.Connection) -> None:
    columns = _column_names(bind, "prescriptions")
    if "branch_id" not in columns:
        with op.batch_alter_table("prescriptions") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "branch_id",
                    UUID(as_uuid=True),
                    nullable=True,
                    comment=(
                        "Branch/facility that created/owns this prescription"
                    ),
                )
            )

        op.execute(
            sa.text(
                """
                UPDATE prescriptions
                   SET branch_id = (
                       SELECT branches.id
                         FROM branches
                        WHERE branches.organization_id =
                              prescriptions.organization_id
                        ORDER BY branches.is_deleted ASC,
                                 branches.created_at ASC,
                                 branches.id ASC
                        LIMIT 1
                   )
                 WHERE branch_id IS NULL
                """
            )
        )
        missing_branch_count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM prescriptions WHERE branch_id IS NULL"
            )
        ).scalar_one()
        if missing_branch_count:
            raise RuntimeError(
                "Cannot assign prescriptions to branches: "
                f"{missing_branch_count} prescription(s) belong to an "
                "organization without a branch"
            )

        with op.batch_alter_table("prescriptions") as batch_op:
            batch_op.alter_column(
                "branch_id",
                existing_type=UUID(as_uuid=True),
                nullable=False,
            )

    foreign_keys = sa.inspect(bind).get_foreign_keys("prescriptions")
    has_branch_fk = any(
        foreign_key.get("constrained_columns") == ["branch_id"]
        and foreign_key.get("referred_table") == "branches"
        for foreign_key in foreign_keys
    )
    if not has_branch_fk:
        with op.batch_alter_table("prescriptions") as batch_op:
            batch_op.create_foreign_key(
                "fk_prescriptions_branch_id_branches",
                "branches",
                ["branch_id"],
                ["id"],
                ondelete="RESTRICT",
            )

    indexes = _index_names(bind, "prescriptions")
    if "idx_prescription_branch" not in indexes:
        op.create_index(
            "idx_prescription_branch",
            "prescriptions",
            ["branch_id"],
        )
    if "ix_prescriptions_branch_id" not in indexes:
        op.create_index(
            "ix_prescriptions_branch_id",
            "prescriptions",
            ["branch_id"],
        )


def _repair_sales_columns(bind: sa.Connection) -> None:
    columns = _column_names(bind, "sales")
    additions = (
        sa.Column(
            "contract_type",
            sa.String(length=50),
            nullable=True,
            comment=(
                "Snapshot of contract type (insurance, corporate, staff, etc.)"
            ),
        ),
        sa.Column(
            "split_payment_details",
            JSONB(),
            nullable=True,
            comment="Split payment breakdown: {method: amount}",
        ),
        sa.Column(
            "insurance_preauth_number",
            sa.String(length=100),
            nullable=True,
            comment="Insurance pre-authorization number",
        ),
        sa.Column(
            "refunded_by",
            UUID(as_uuid=True),
            nullable=True,
            comment="User who processed the refund",
        ),
        sa.Column(
            "refund_reason",
            sa.Text(),
            nullable=True,
            comment="Reason for the refund",
        ),
        sa.Column(
            "refund_reference",
            sa.String(length=100),
            nullable=True,
            comment="Unique refund transaction reference",
        ),
    )
    foreign_keys = sa.inspect(bind).get_foreign_keys("sales")
    has_refunded_by_fk = any(
        foreign_key.get("constrained_columns") == ["refunded_by"]
        and foreign_key.get("referred_table") == "users"
        for foreign_key in foreign_keys
    )
    with op.batch_alter_table("sales") as batch_op:
        for column in additions:
            if column.name not in columns:
                batch_op.add_column(column)
        if not has_refunded_by_fk:
            batch_op.create_foreign_key(
                "fk_sales_refunded_by_users",
                "users",
                ["refunded_by"],
                ["id"],
                ondelete="SET NULL",
            )


def _repair_scoped_indexes(bind: sa.Connection) -> None:
    tables = set(sa.inspect(bind).get_table_names())
    for table, global_name, scoped_name, columns in SCOPED_INDEXES:
        if table not in tables:
            continue
        indexes = _index_names(bind, table)
        if scoped_name not in indexes:
            op.create_index(
                scoped_name,
                table,
                list(columns),
                unique=True,
            )
        if global_name in indexes:
            op.drop_index(global_name, table_name=table)


def _repair_user_indexes(bind: sa.Connection) -> None:
    indexes = _index_names(bind, "users")
    for index_name in (
        "idx_user_super_admin",
        "ix_users_is_super_admin",
    ):
        if index_name not in indexes:
            op.create_index(
                index_name,
                "users",
                ["is_super_admin"],
            )


def _align_comments() -> None:
    comments = (
        (
            "price_contracts",
            "requires_approval",
            sa.Boolean(),
            "Require manager approval before applying during checkout",
        ),
        (
            "price_contracts",
            "daily_usage_limit",
            sa.Integer(),
            "Maximum number of completed sales that may use this contract per day",
        ),
        (
            "price_contracts",
            "per_customer_usage_limit",
            sa.Integer(),
            (
                "Maximum completed sales a single customer may process with "
                "this contract"
            ),
        ),
        (
            "price_contracts",
            "requires_preauthorization",
            sa.Boolean(),
            (
                "Require a pre-authorization number before applying this "
                "contract"
            ),
        ),
        (
            "sale_items",
            "refunded_quantity",
            sa.Integer(),
            "Cumulative units refunded from this sale line",
        ),
        (
            "users",
            "is_super_admin",
            sa.Boolean(),
            "Hardcoded super admin flag with absolute access",
        ),
        (
            "users",
            "two_factor_secret",
            sa.String(length=255),
            "Base32-encoded TOTP secret",
        ),
        (
            "users",
            "must_change_password",
            sa.Boolean(),
            "Newly created users must change password on first login",
        ),
    )
    for table, column, column_type, comment in comments:
        op.alter_column(
            table,
            column,
            existing_type=column_type,
            comment=comment,
        )


def upgrade() -> None:
    """Repair every actionable difference reported by Alembic."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if {"prescriptions", "branches"}.issubset(tables):
        _repair_prescription_branch(bind)
    if {"sales", "users"}.issubset(tables):
        _repair_sales_columns(bind)
    _repair_scoped_indexes(bind)
    if "users" in tables and "is_super_admin" in _column_names(bind, "users"):
        _repair_user_indexes(bind)
    if bind.dialect.name == "postgresql":
        _align_comments()


def downgrade() -> None:
    # These objects are owned by earlier migrations or current application
    # models. Removing them here would reintroduce production schema drift.
    pass
