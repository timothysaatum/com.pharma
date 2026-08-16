"""cast user_roles and rbac columns to native uuid in postgres

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-16 08:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                -- 1. Repair roles table columns if stored as character varying
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'roles'
                      AND column_name = 'id'
                      AND data_type LIKE '%char%'
                ) THEN
                    ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_role_id_fkey;
                    ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_organization_id_fkey;
                    ALTER TABLE roles ALTER COLUMN id TYPE UUID USING id::uuid;
                    ALTER TABLE roles ALTER COLUMN organization_id TYPE UUID USING organization_id::uuid;
                    ALTER TABLE roles ADD CONSTRAINT roles_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;
                END IF;

                -- 2. Repair user_roles table columns if stored as character varying
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'user_roles'
                      AND column_name = 'user_id'
                      AND data_type LIKE '%char%'
                ) THEN
                    ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;
                    ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_role_id_fkey;
                    ALTER TABLE user_roles ALTER COLUMN user_id TYPE UUID USING user_id::uuid;
                    ALTER TABLE user_roles ALTER COLUMN role_id TYPE UUID USING role_id::uuid;
                    ALTER TABLE user_roles ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
                    ALTER TABLE user_roles ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE;
                ELSIF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'user_roles'
                      AND column_name = 'role_id'
                      AND data_type LIKE '%char%'
                ) THEN
                    ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_role_id_fkey;
                    ALTER TABLE user_roles ALTER COLUMN role_id TYPE UUID USING role_id::uuid;
                    ALTER TABLE user_roles ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE;
                END IF;

                -- 3. Repair user_sessions table if user_id is character varying
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'user_sessions'
                      AND column_name = 'user_id'
                      AND data_type LIKE '%char%'
                ) THEN
                    ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_user_id_fkey;
                    ALTER TABLE user_sessions ALTER COLUMN id TYPE UUID USING id::uuid;
                    ALTER TABLE user_sessions ALTER COLUMN user_id TYPE UUID USING user_id::uuid;
                    ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    pass
