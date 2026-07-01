"""repair legacy user role schema drift

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-01 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_column_dependencies(
    inspector: sa.Inspector,
    column_name: str,
) -> None:
    """Drop named indexes and checks that depend on a legacy column."""
    for index in inspector.get_indexes("users"):
        if column_name in (index.get("column_names") or []):
            op.drop_index(index["name"], table_name="users")

    for constraint in inspector.get_check_constraints("users"):
        sqltext = (constraint.get("sqltext") or "").lower()
        name = constraint.get("name")
        if name and column_name.lower() in sqltext:
            op.drop_constraint(name, "users", type_="check")


def upgrade() -> None:
    """Remove legacy role fields left behind on stamped databases."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    table_names = set(inspector.get_table_names())

    if (
        "role" in columns
        and "roles" in table_names
        and "user_roles" in table_names
    ):
        # Preserve legacy assignments whenever the organization already has a
        # matching RBAC role. Existing junction rows remain untouched.
        op.execute(
            sa.text(
                """
                INSERT INTO user_roles (user_id, role_id, created_at, updated_at)
                SELECT users.id, roles.id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM users
                JOIN roles
                  ON roles.organization_id = users.organization_id
                 AND lower(roles.name) = lower(users.role)
                WHERE users.role IS NOT NULL
                ON CONFLICT (user_id, role_id) DO NOTHING
                """
            )
        )

    # The current User model and RBAC service no longer write either field.
    # Keeping a NOT NULL legacy column makes every new user INSERT fail.
    for column_name in ("role", "permissions"):
        if column_name not in columns:
            continue
        inspector = sa.inspect(bind)
        _drop_column_dependencies(inspector, column_name)
        op.drop_column("users", column_name)


def downgrade() -> None:
    # Reintroducing these columns would create two competing sources of role
    # authority and cannot faithfully reconstruct legacy permission overrides.
    pass
