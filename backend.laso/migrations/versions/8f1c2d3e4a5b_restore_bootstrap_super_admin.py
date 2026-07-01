"""restore bootstrap super admin authority

Revision ID: 8f1c2d3e4a5b
Revises: 2dac2bae15f3
Create Date: 2026-07-01 18:00:00.000000

The role-to-RBAC migration introduced ``is_super_admin`` with a false default
and then dropped the legacy ``role`` column. That demoted the bootstrap
administrator because the old ``super_admin`` value was never copied.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8f1c2d3e4a5b"
down_revision: Union[str, None] = "2dac2bae15f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    users = sa.table(
        "users",
        sa.column("username", sa.String()),
        sa.column("full_name", sa.String()),
        sa.column("is_super_admin", sa.Boolean()),
    )

    op.execute(
        users.update()
        .where(
            sa.and_(
                users.c.username == "admin",
                users.c.full_name == "System Administrator",
                users.c.is_super_admin.is_(False),
            )
        )
        .values(is_super_admin=True)
    )


def downgrade() -> None:
    # Deliberately preserve authority on downgrade. Automatically demoting a
    # legitimate platform administrator would be unsafe and could lock out the
    # deployment.
    pass
