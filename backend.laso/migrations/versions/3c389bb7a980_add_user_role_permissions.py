"""
add user role-permissions

Revision ID: 3c389bb7a980
Revises: 02b7b3b2dfb1
Create Date: 2026-06-23 10:44:15.201753
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app

revision: str = '3c389bb7a980'
down_revision: Union[str, None] = '02b7b3b2dfb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================
    # ROLES TABLE
    # =========================
    op.create_table(
        'roles',
        sa.Column('id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('organization_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('permissions', app.models.db_types.ARRAY(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sync_version', sa.BigInteger(), nullable=False,
                  comment='Incremented on each update for conflict detection'),
        sa.Column('sync_status', sa.String(length=20), nullable=False,
                  comment='synced, pending, conflict, deleted'),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True,
                  comment='Last successful sync with server'),
        sa.Column('sync_hash', sa.String(length=64), nullable=True,
                  comment='SHA256 hash for detecting changes'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    with op.batch_alter_table('roles') as batch_op:
        batch_op.create_index('idx_role_org', ['organization_id'])
        batch_op.create_index('ix_roles_created_at', ['created_at'])
        batch_op.create_index('ix_roles_organization_id', ['organization_id'])
        batch_op.create_index('ix_roles_sync_status', ['sync_status'])
        batch_op.create_index('ix_roles_updated_at', ['updated_at'])
        batch_op.create_index('uq_role_org_name', ['organization_id', 'name'], unique=True)

    # =========================
    # USER_ROLES TABLE
    # =========================
    op.create_table(
        'user_roles',
        sa.Column('user_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('role_id', app.models.db_types.UUID(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'role_id')
    )

    with op.batch_alter_table('user_roles') as batch_op:
        batch_op.create_index('ix_user_roles_created_at', ['created_at'])
        batch_op.create_index('ix_user_roles_updated_at', ['updated_at'])

    # =========================
    # USERS TABLE FIX (IMPORTANT PART)
    # =========================
    with op.batch_alter_table('users') as batch_op:
        # 1. ADD COLUMN SAFE (TEMP DEFAULT)
        batch_op.add_column(
            sa.Column(
                'is_super_admin',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment='Hardcoded super admin flag with absolute access'
            )
        )

        # 2. DROP OLD INDEXES
        batch_op.drop_index('idx_user_role')
        batch_op.drop_index('ix_users_role')

        # 3. NEW INDEXES
        batch_op.create_index('idx_user_super_admin', ['is_super_admin'])
        batch_op.create_index('ix_users_is_super_admin', ['is_super_admin'])

        # 4. DROP OLD COLUMNS
        batch_op.drop_column('permissions')
        batch_op.drop_column('role')

    # OPTIONAL CLEANUP (remove server default after migration)
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('is_super_admin', server_default=None)


def downgrade() -> None:
    # =========================
    # USERS ROLLBACK
    # =========================
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(
            sa.Column(
                'role',
                sa.VARCHAR(length=50),
                nullable=False,
                comment='super_admin, admin, manager, pharmacist, cashier, viewer'
            )
        )
        batch_op.add_column(
            sa.Column(
                'permissions',
                sa.TEXT(),
                nullable=False,
                comment="{ additional: ['perm1', 'perm2'], denied: ['perm3'] }"
            )
        )

        batch_op.drop_index('ix_users_is_super_admin')
        batch_op.drop_index('idx_user_super_admin')

        batch_op.create_index('ix_users_role', ['role'])
        batch_op.create_index('idx_user_role', ['role'])

        batch_op.drop_column('is_super_admin')

    # =========================
    # USER_ROLES ROLLBACK
    # =========================
    with op.batch_alter_table('user_roles') as batch_op:
        batch_op.drop_index('ix_user_roles_updated_at')
        batch_op.drop_index('ix_user_roles_created_at')

    op.drop_table('user_roles')

    # =========================
    # ROLES ROLLBACK
    # =========================
    with op.batch_alter_table('roles') as batch_op:
        batch_op.drop_index('uq_role_org_name')
        batch_op.drop_index('ix_roles_updated_at')
        batch_op.drop_index('ix_roles_sync_status')
        batch_op.drop_index('ix_roles_organization_id')
        batch_op.drop_index('ix_roles_created_at')
        batch_op.drop_index('idx_role_org')

    op.drop_table('roles')
