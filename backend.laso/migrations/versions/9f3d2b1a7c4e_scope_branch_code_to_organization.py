"""scope branch code uniqueness to organization

Revision ID: 9f3d2b1a7c4e
Revises: 4a8c7a6b5ba3
Create Date: 2026-05-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f3d2b1a7c4e'
down_revision: Union[str, None] = '4a8c7a6b5ba3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table('branches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_branches_code'))
        batch_op.create_index(batch_op.f('ix_branches_code'), ['code'], unique=False)

        if dialect == 'sqlite':
            batch_op.create_index(
                'uq_branch_org_code_active',
                ['organization_id', 'code'],
                unique=True,
                sqlite_where=sa.text('is_deleted = 0'),
            )
        elif dialect == 'postgresql':
            batch_op.create_index(
                'uq_branch_org_code_active',
                ['organization_id', 'code'],
                unique=True,
                postgresql_where=sa.text('is_deleted = false'),
            )
        else:
            batch_op.create_index(
                'uq_branch_org_code_active',
                ['organization_id', 'code'],
                unique=True,
            )


def downgrade() -> None:
    with op.batch_alter_table('branches', schema=None) as batch_op:
        batch_op.drop_index('uq_branch_org_code_active')
        batch_op.drop_index(batch_op.f('ix_branches_code'))
        batch_op.create_index(batch_op.f('ix_branches_code'), ['code'], unique=True)
