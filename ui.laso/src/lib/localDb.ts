/**
 * localDb.ts
 * ==========
 * Local SQLite database via tauri-plugin-sql.
 * Mirrors the server's schema for the tables the branch owns or caches.
 *
 * Install:  cargo add tauri-plugin-sql --features sqlite
 *           pnpm add @tauri-apps/plugin-sql
 *
 * Tauri v2 src-tauri/lib.rs — register the plugin:
 *   .plugin(tauri_plugin_sql::Builder::new()
 *     .add_migrations("sqlite:laso.db", migrations)
 *     .build())
 */

import Database from "@tauri-apps/plugin-sql";

const DB_PATH = "sqlite:laso.db";
let _db: Database | null = null;

/** Get (or lazily open) the local database connection. */
export async function getDb(): Promise<Database> {
  if (_db) return _db;
  _db = await Database.load(DB_PATH);
  await runMigrations(_db);
  return _db;
}

// ─────────────────────────────────────────────────────────────────────────────
// SCHEMA MIGRATIONS
// Each migration runs exactly once, tracked by user_version pragma.
// ─────────────────────────────────────────────────────────────────────────────

async function runMigrations(db: Database): Promise<void> {
  const [{ user_version }] = await db.select<{ user_version: number }[]>(
    "PRAGMA user_version"
  );

  if (user_version < 1) await migrate_v1(db);
  if (user_version < 2) await migrate_v2(db);
  if (user_version < 3) await migrate_v3(db);
  if (user_version < 4) await migrate_v4(db);
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V1 — initial schema
// Sales table aligned to the rewritten Sale model: single discount_amount
// field (not three separate fields), real prescription/receipt columns added,
// phantom pre-rewrite columns removed.
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v1(db: Database): Promise<void> {
  // ── Org-level tables (pull-only, never written locally except reads) ──

  await db.execute(`
    CREATE TABLE IF NOT EXISTS drugs (
      id                TEXT PRIMARY KEY,
      organization_id   TEXT NOT NULL,
      name              TEXT NOT NULL,
      generic_name      TEXT,
      brand_name        TEXT,
      sku               TEXT,
      barcode           TEXT,
      category_id       TEXT,
      drug_type         TEXT NOT NULL DEFAULT 'otc',
      dosage_form       TEXT,
      strength          TEXT,
      manufacturer      TEXT,
      supplier          TEXT,
      requires_prescription           INTEGER NOT NULL DEFAULT 0,
      controlled_substance_schedule   TEXT,
      ndc_code                        TEXT,
      unit_price                      REAL NOT NULL,
      cost_price                      REAL,
      markup_percentage               REAL,
      tax_rate                        REAL NOT NULL DEFAULT 0,
      reorder_level                   INTEGER NOT NULL DEFAULT 10,
      reorder_quantity                INTEGER NOT NULL DEFAULT 50,
      max_stock_level                 INTEGER,
      unit_of_measure                 TEXT NOT NULL DEFAULT 'unit',
      description                     TEXT,
      usage_instructions              TEXT,
      side_effects                    TEXT,
      contraindications               TEXT,
      storage_conditions              TEXT,
      is_active                       INTEGER NOT NULL DEFAULT 1,
      is_deleted        INTEGER NOT NULL DEFAULT 0,
      sync_status       TEXT NOT NULL DEFAULT 'synced',
      sync_version      INTEGER NOT NULL DEFAULT 1,
      synced_at         TEXT,
      updated_at        TEXT NOT NULL,
      created_at        TEXT NOT NULL
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS drug_categories (
      id              TEXT PRIMARY KEY,
      organization_id TEXT NOT NULL,
      name            TEXT NOT NULL,
      description     TEXT,
      parent_id       TEXT,
      path            TEXT,
      level           INTEGER NOT NULL DEFAULT 0,
      is_deleted      INTEGER NOT NULL DEFAULT 0,
      sync_status     TEXT NOT NULL DEFAULT 'synced',
      sync_version    INTEGER NOT NULL DEFAULT 1,
      synced_at       TEXT,
      updated_at      TEXT NOT NULL,
      created_at      TEXT NOT NULL
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS price_contracts (
      id                        TEXT PRIMARY KEY,
      organization_id           TEXT NOT NULL,
      contract_code             TEXT NOT NULL,
      contract_name             TEXT NOT NULL,
      contract_type             TEXT NOT NULL,
      is_default_contract       INTEGER NOT NULL DEFAULT 0,
      discount_type             TEXT NOT NULL DEFAULT 'percentage',
      discount_percentage       REAL NOT NULL DEFAULT 0,
      applies_to_prescription_only INTEGER NOT NULL DEFAULT 0,
      applies_to_otc            INTEGER NOT NULL DEFAULT 1,
      applies_to_all_branches   INTEGER NOT NULL DEFAULT 1,
      applicable_branch_ids     TEXT NOT NULL DEFAULT '[]',
      effective_from            TEXT NOT NULL,
      effective_to              TEXT,
      status                    TEXT NOT NULL DEFAULT 'active',
      is_active                 INTEGER NOT NULL DEFAULT 1,
      copay_amount              REAL,
      copay_percentage          REAL,
      is_deleted                INTEGER NOT NULL DEFAULT 0,
      sync_status               TEXT NOT NULL DEFAULT 'synced',
      sync_version              INTEGER NOT NULL DEFAULT 1,
      synced_at                 TEXT,
      updated_at                TEXT NOT NULL,
      created_at                TEXT NOT NULL
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS customers (
      id                      TEXT PRIMARY KEY,
      organization_id         TEXT NOT NULL,
      customer_type           TEXT NOT NULL DEFAULT 'walk_in',
      first_name              TEXT,
      last_name               TEXT,
      phone                   TEXT,
      email                   TEXT,
      date_of_birth           TEXT,
      loyalty_points          INTEGER NOT NULL DEFAULT 0,
      loyalty_tier            TEXT NOT NULL DEFAULT 'bronze',
      insurance_provider_id   TEXT,
      insurance_member_id     TEXT,
      preferred_contract_id   TEXT,
      is_active               INTEGER NOT NULL DEFAULT 1,
      is_deleted              INTEGER NOT NULL DEFAULT 0,
      sync_status             TEXT NOT NULL DEFAULT 'synced',
      sync_version            INTEGER NOT NULL DEFAULT 1,
      synced_at               TEXT,
      updated_at              TEXT NOT NULL,
      created_at              TEXT NOT NULL
    )
  `);

  // ── Branch-level tables (read/write locally, pushed to server) ──

  await db.execute(`
    CREATE TABLE IF NOT EXISTS branch_inventory (
      id                TEXT PRIMARY KEY,
      branch_id         TEXT NOT NULL,
      drug_id           TEXT NOT NULL,
      quantity          INTEGER NOT NULL DEFAULT 0,
      reserved_quantity INTEGER NOT NULL DEFAULT 0,
      location          TEXT,
      selling_price     REAL,
      sync_status       TEXT NOT NULL DEFAULT 'synced',
      sync_version      INTEGER NOT NULL DEFAULT 1,
      synced_at         TEXT,
      updated_at        TEXT NOT NULL,
      created_at        TEXT NOT NULL,
      UNIQUE(branch_id, drug_id)
    )
  `);
  await db.execute(`ALTER TABLE branch_inventory ADD COLUMN selling_price REAL`).catch(() => undefined);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS drug_batches (
      id                  TEXT PRIMARY KEY,
      branch_id           TEXT NOT NULL,
      drug_id             TEXT NOT NULL,
      batch_number        TEXT NOT NULL,
      quantity            INTEGER NOT NULL,
      remaining_quantity  INTEGER NOT NULL,
      manufacturing_date  TEXT,
      expiry_date         TEXT NOT NULL,
      cost_price          REAL,
      selling_price       REAL,
      supplier            TEXT,
      purchase_order_id   TEXT,
      sync_status         TEXT NOT NULL DEFAULT 'synced',
      sync_version        INTEGER NOT NULL DEFAULT 1,
      synced_at           TEXT,
      updated_at          TEXT NOT NULL,
      created_at          TEXT NOT NULL
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS sales (
      id                            TEXT PRIMARY KEY,
      organization_id               TEXT NOT NULL,
      branch_id                     TEXT NOT NULL,
      sale_number                   TEXT NOT NULL UNIQUE,
      customer_id                   TEXT,
      customer_name                 TEXT,

      -- Financials — aligned to rewritten Sale model (single discount_amount)
      subtotal                      REAL NOT NULL,
      discount_amount               REAL NOT NULL DEFAULT 0,
      tax_amount                    REAL NOT NULL DEFAULT 0,
      total_amount                  REAL NOT NULL,

      -- Contract snapshot
      price_contract_id             TEXT,
      contract_name                 TEXT,
      contract_discount_percentage  REAL,

      -- Payment
      payment_method                TEXT NOT NULL DEFAULT 'cash',
      payment_status                TEXT NOT NULL DEFAULT 'completed',
      amount_paid                   REAL,
      change_amount                 REAL NOT NULL DEFAULT 0,
      payment_reference             TEXT,

      -- Prescription
      prescription_id               TEXT,
      prescription_number           TEXT,
      prescriber_name               TEXT,

      -- Staff
      cashier_id                    TEXT NOT NULL,
      pharmacist_id                 TEXT,

      -- Insurance
      insurance_claim_number        TEXT,
      patient_copay_amount          REAL,
      insurance_covered_amount      REAL,
      insurance_verified            INTEGER NOT NULL DEFAULT 0,
      insurance_verified_at         TEXT,
      insurance_verified_by         TEXT,

      -- Status and audit
      notes                         TEXT,
      status                        TEXT NOT NULL DEFAULT 'completed',
      cancelled_at                  TEXT,
      cancelled_by                  TEXT,
      cancellation_reason           TEXT,
      refund_amount                 REAL,
      refunded_at                   TEXT,

      -- Receipt
      receipt_printed               INTEGER NOT NULL DEFAULT 0,
      receipt_emailed               INTEGER NOT NULL DEFAULT 0,

      -- Local-only: sale items stored as JSON for offline receipt display.
      -- Not returned by the server pull — written only by localWrite.sale.
      items_json                    TEXT DEFAULT '[]',

      -- Sync
      sync_status                   TEXT NOT NULL DEFAULT 'pending',
      sync_version                  INTEGER NOT NULL DEFAULT 1,
      synced_at                     TEXT,
      updated_at                    TEXT NOT NULL,
      created_at                    TEXT NOT NULL
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS purchase_orders (
      id                    TEXT PRIMARY KEY,
      organization_id       TEXT NOT NULL,
      branch_id             TEXT NOT NULL,
      po_number             TEXT NOT NULL,
      supplier_id           TEXT NOT NULL,
      subtotal              REAL NOT NULL DEFAULT 0,
      tax_amount            REAL NOT NULL DEFAULT 0,
      shipping_cost         REAL NOT NULL DEFAULT 0,
      total_amount          REAL NOT NULL DEFAULT 0,
      status                TEXT NOT NULL DEFAULT 'draft',
      ordered_by            TEXT NOT NULL,
      approved_by           TEXT,
      approved_at           TEXT,
      expected_delivery_date TEXT,
      received_date         TEXT,
      notes                 TEXT,
      items_json            TEXT NOT NULL DEFAULT '[]',
      sync_status           TEXT NOT NULL DEFAULT 'pending',
      sync_version          INTEGER NOT NULL DEFAULT 1,
      synced_at             TEXT,
      updated_at            TEXT NOT NULL,
      created_at            TEXT NOT NULL
    )
  `);

  // ── Sync queue — tracks pending push operations ──

  await db.execute(`
    CREATE TABLE IF NOT EXISTS sync_queue (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      table_name      TEXT NOT NULL,
      record_id       TEXT NOT NULL,
      operation       TEXT NOT NULL DEFAULT 'create',
      sync_version    INTEGER NOT NULL DEFAULT 1,
      payload_json    TEXT NOT NULL,
      created_offline_at TEXT NOT NULL,
      attempts        INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TEXT,
      error           TEXT,
      UNIQUE(table_name, record_id)
    )
  `);

  // ── Sync metadata ──

  await db.execute(`
    CREATE TABLE IF NOT EXISTS sync_meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
  `);

  // ── Indexes ──

  await db.execute(`CREATE INDEX IF NOT EXISTS idx_drugs_org      ON drugs(organization_id)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_drugs_active   ON drugs(is_active)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_drugs_type     ON drugs(drug_type)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_inv_branch     ON branch_inventory(branch_id)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_batch_branch   ON drug_batches(branch_id)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_batch_expiry   ON drug_batches(expiry_date)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_sales_branch   ON sales(branch_id)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_sales_status   ON sales(sync_status)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_queue_table    ON sync_queue(table_name)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_customer_phone ON customers(phone)`);
  await db.execute(`CREATE INDEX IF NOT EXISTS idx_customer_email ON customers(email)`);

  await db.execute("PRAGMA user_version = 1");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V2 — upgrade from old v1 schema
//
// Old v1 (pre-rewrite) had the three phantom discount fields and was missing
// the six real Sale model columns below.  This migration adds the missing
// real columns for existing installs.
//
// All ADD COLUMN calls are wrapped in try/catch so they are safe to run on
// both old and new installs (fresh installs already have these columns from
// the corrected v1 above; the errors are silently swallowed).
//
// Phantom columns from old v1 (contract_discount_amount, cashier_name, etc.)
// cannot be dropped in SQLite < 3.35 without a full table rebuild, so they
// are left in place on old installs but never written to.
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v2(db: Database): Promise<void> {
  const addColumn = async (col: string) => {
    try { await db.execute(`ALTER TABLE sales ADD COLUMN ${col}`); }
    catch { /* column already exists on fresh installs — safe to ignore */ }
  };

  // The single discount field that replaced the three phantom fields
  await addColumn("discount_amount               REAL NOT NULL DEFAULT 0");

  // Prescription snapshot fields
  await addColumn("prescription_number           TEXT");
  await addColumn("prescriber_name               TEXT");

  // Receipt tracking
  await addColumn("receipt_printed               INTEGER NOT NULL DEFAULT 0");
  await addColumn("receipt_emailed               INTEGER NOT NULL DEFAULT 0");

  await db.execute("PRAGMA user_version = 2");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V3 — upgrade from old v2 schema
//
// Installs that already ran the old v2 (user_version = 2) never received the
// six real columns above (old v2 only added phantom fields).  This migration
// adds the same columns for those installs.
//
// Fresh installs and installs that ran new v2 already have all columns;
// the try/catch silently ignores the "column already exists" errors.
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v3(db: Database): Promise<void> {
  const addColumn = async (col: string) => {
    try { await db.execute(`ALTER TABLE sales ADD COLUMN ${col}`); }
    catch { /* already exists — safe to ignore */ }
  };

  await addColumn("discount_amount               REAL NOT NULL DEFAULT 0");
  await addColumn("prescription_number           TEXT");
  await addColumn("prescriber_name               TEXT");
  await addColumn("receipt_printed               INTEGER NOT NULL DEFAULT 0");
  await addColumn("receipt_emailed               INTEGER NOT NULL DEFAULT 0");

  await db.execute("PRAGMA user_version = 3");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V4 — add missing drug columns introduced in DrugForm rewrite
//
// Five columns were added to DrugCreate/DrugUpdate (markup_percentage,
// max_stock_level, ndc_code, controlled_substance_schedule, contraindications)
// but were never reflected in the local drugs table.  Without these columns
// the sync engine's upsert would silently drop the values on every pull.
//
// All ADD COLUMN calls use try/catch so they are safe on both old installs
// (column missing → add it) and fresh installs that already have them via
// the corrected v1 above (duplicate column error → silently ignored).
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v4(db: Database): Promise<void> {
  const addCol = async (col: string) => {
    try { await db.execute(`ALTER TABLE drugs ADD COLUMN ${col}`); }
    catch { /* column already exists on fresh installs — safe to ignore */ }
  };

  await addCol("controlled_substance_schedule TEXT");
  await addCol("ndc_code                      TEXT");
  await addCol("markup_percentage             REAL");
  await addCol("max_stock_level               INTEGER");
  await addCol("contraindications             TEXT");

  await db.execute("PRAGMA user_version = 4");
}

// ─────────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────────

export async function getLastSyncAt(table?: string): Promise<string | null> {
  const db = await getDb();
  const key = table ? `last_sync_at:${table}` : "last_sync_at";
  const rows = await db.select<{ value: string }[]>(
    "SELECT value FROM sync_meta WHERE key = $1",
    [key]
  );
  return rows[0]?.value ?? null;
}

export async function setLastSyncAt(timestamp: string, table?: string): Promise<void> {
  const db = await getDb();
  const key = table ? `last_sync_at:${table}` : "last_sync_at";
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
    [key, timestamp]
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SYNC QUEUE HELPERS
// ─────────────────────────────────────────────────────────────────────────────

export interface QueuedRecord {
  id: number;
  table_name: string;
  record_id: string;
  operation: "create" | "update" | "delete";
  sync_version: number;
  payload_json: string;
  created_offline_at: string;
  attempts: number;
  error: string | null;
}

/** Add or replace a record in the push queue. */
export async function enqueue(
  tableName: string,
  recordId: string,
  operation: "create" | "update" | "delete",
  syncVersion: number,
  payload: Record<string, unknown>
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `INSERT INTO sync_queue
       (table_name, record_id, operation, sync_version, payload_json, created_offline_at)
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT(table_name, record_id) DO UPDATE SET
       operation    = excluded.operation,
       sync_version = excluded.sync_version,
       payload_json = excluded.payload_json,
       attempts     = 0,
       error        = NULL`,
    [tableName, recordId, operation, syncVersion, JSON.stringify(payload), new Date().toISOString()]
  );
}

/** Get all records pending push, oldest first. */
export async function getPendingQueue(limit = 500): Promise<QueuedRecord[]> {
  const db = await getDb();
  return db.select<QueuedRecord[]>(
    "SELECT * FROM sync_queue ORDER BY id ASC LIMIT $1",
    [limit]
  );
}

/** Mark a queued record as successfully synced (remove it). */
export async function dequeue(tableName: string, recordId: string): Promise<void> {
  const db = await getDb();
  await db.execute(
    "DELETE FROM sync_queue WHERE table_name = $1 AND record_id = $2",
    [tableName, recordId]
  );
}

/** Record a failed push attempt. */
export async function markQueueError(
  tableName: string,
  recordId: string,
  error: string
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `UPDATE sync_queue
     SET attempts = attempts + 1, last_attempt_at = $1, error = $2
     WHERE table_name = $3 AND record_id = $4`,
    [new Date().toISOString(), error, tableName, recordId]
  );
}

/** Total number of records waiting to be pushed. */
export async function getPendingCount(): Promise<number> {
  const db = await getDb();
  const [row] = await db.select<{ count: number }[]>(
    "SELECT COUNT(*) as count FROM sync_queue"
  );
  return row?.count ?? 0;
}
