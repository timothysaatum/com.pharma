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
            DECLARE
                r RECORD;
                target_tables TEXT[] := ARRAY[
                    'organizations', 'branches', 'users', 'roles', 'user_roles', 'user_sessions',
                    'drug_categories', 'drugs', 'suppliers', 'customers', 'insurance_providers',
                    'prescriptions', 'price_contracts', 'price_contract_items', 'purchase_orders',
                    'purchase_order_items', 'branch_inventory', 'drug_batches', 'sales',
                    'sale_items', 'sale_item_batch_allocations', 'stock_adjustments',
                    'inventory_movements', 'audit_logs', 'system_alerts', 'sync_operation_receipts',
                    'sync_queue', 'crr_branch_sync_watermark', 'stock_leases'
                ];
                target_cols TEXT[] := ARRAY[
                    'id', 'organization_id', 'branch_id', 'drug_id', 'batch_id', 'customer_id',
                    'cashier_id', 'pharmacist_id', 'prescription_id', 'price_contract_id',
                    'refunded_by', 'cancelled_by', 'sale_id', 'sale_item_id', 'purchase_order_id',
                    'supplier_id', 'ordered_by', 'approved_by', 'manager_id', 'deleted_by',
                    'parent_id', 'category_id', 'insurance_provider_id', 'preferred_contract_id',
                    'verified_by', 'contract_id', 'adjusted_by', 'transfer_to_branch_id',
                    'source_id', 'source_line_id', 'created_by', 'user_id', 'role_id',
                    'entity_id', 'resolved_by', 'operation_id'
                ];
            BEGIN
                -- 1. Save all foreign key constraints in current schema to a temporary table
                CREATE TEMP TABLE IF NOT EXISTS _tmp_saved_fks (
                    table_name text,
                    constraint_name text,
                    constraint_def text
                ) ON COMMIT DROP;

                DELETE FROM _tmp_saved_fks;

                INSERT INTO _tmp_saved_fks (table_name, constraint_name, constraint_def)
                SELECT
                    c.conrelid::regclass::text,
                    c.conname,
                    pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE c.contype = 'f'
                  AND n.nspname = current_schema();

                -- 2. Drop all foreign key constraints so column type changes don't fail constraint validation
                FOR r IN SELECT table_name, constraint_name FROM _tmp_saved_fks LOOP
                    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I', r.table_name, r.constraint_name);
                END LOOP;

                -- 3. Alter all target UUID columns that are stored as VARCHAR / TEXT to native PostgreSQL UUID
                FOR r IN (
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = ANY(target_tables)
                      AND column_name = ANY(target_cols)
                      AND (data_type LIKE '%char%' OR data_type = 'text')
                ) LOOP
                    BEGIN
                        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE UUID USING NULLIF(%I, '''')::uuid', r.table_name, r.column_name, r.column_name);
                    EXCEPTION WHEN OTHERS THEN
                        RAISE NOTICE 'Skipping column %.%: %', r.table_name, r.column_name, SQLERRM;
                    END;
                END LOOP;

                -- 4. Re-create all foreign key constraints
                FOR r IN SELECT table_name, constraint_name, constraint_def FROM _tmp_saved_fks LOOP
                    BEGIN
                        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.table_name, r.constraint_name, r.constraint_def);
                    EXCEPTION WHEN OTHERS THEN
                        RAISE NOTICE 'Could not restore FK constraint % on %: %', r.constraint_name, r.table_name, SQLERRM;
                    END;
                END LOOP;
            END $$;
            """
        )
    )


def downgrade() -> None:
    pass
