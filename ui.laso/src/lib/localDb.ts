/**
 * localDb.ts
 * ==========
 * Local SQLite database via tauri-plugin-sql.
 * Mirrors the server's schema for the tables the branch owns or caches.
 */

import type { BranchInventoryWithDetails, PushConflict, Drug, Sale } from "@/types";
import { RetryBackoff } from "@/lib/syncRetryBackoff";
import { invoke } from "@tauri-apps/api/core";

const IS_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

interface ExecResult { rowsAffected: number; lastInsertId?: number }

export interface DbTransactionStatement {
  sql: string;
  values?: unknown[];
  expectedRows?: number;
  errorMessage?: string;
}

// Define a minimal interface for the Database object we use
export interface Database {
  execute(query: string, values?: unknown[]): Promise<ExecResult>;
  select<T>(query: string, values?: unknown[]): Promise<T>;
  execute_batch(query: string): Promise<void>;
  executeTransaction?(statements: DbTransactionStatement[]): Promise<ExecResult[]>;
  load(path: string): Promise<Database>;
}

/** Mock database for browser environment */
const MockDb: Database = {
  execute: async () => ({ rowsAffected: 0 }),
  select: async <T>(_: string): Promise<T> => ([] as unknown as T),
  execute_batch: async (_: string) => {},
  executeTransaction: async () => {
    throw new Error("Durable offline checkout is available in the desktop app only.");
  },
  load: async () => MockDb,
};

let _db: Database | null = null;
// Caches the IN-FLIGHT initialization, not just the resolved connection.
// getDb() can be called concurrently — e.g. SyncEngine.start() fires
// loadPersistedQueueState() without awaiting it, then immediately calls
// sync(), which calls getDb() again while the first call's runMigrations()
// may still be running (a schema migration on a fresh/older device can take
// long enough to make this race easy to hit, not just theoretical). Every
// individual db.execute()/db.execute_batch() call is its own separate Tauri
// IPC round-trip, and the Rust-side connection Mutex is only held for one
// statement at a time — NOT for a whole logical "BEGIN ... COMMIT" spanning
// several awaited calls. Two concurrent callers, each stepping through their
// own multi-statement transaction one IPC call at a time, can interleave on
// the shared connection, and whichever side's statement lands while the
// other's transaction is still open fails with the raw SQLite error "cannot
// start a transaction within a transaction". Caching the in-flight promise
// (not just the eventual value) makes every concurrent caller await the
// SAME single initialization instead of racing a second one in underneath
// it — this is the actual fix; there was previously nothing serializing
// concurrent getDb() calls during startup.
let _dbPromise: Promise<Database> | null = null;

/** Get (or lazily open) the local database connection. */
export function getDb(): Promise<Database> {
  if (_db) return Promise.resolve(_db);
  if (_dbPromise) return _dbPromise;

  _dbPromise = initDb().catch((err) => {
    // Allow a later call to retry a fresh initialization instead of every
    // subsequent getDb() call for the rest of the session repeating the
    // same failure forever.
    _dbPromise = null;
    throw err;
  });
  return _dbPromise;
}

async function initDb(): Promise<Database> {
  if (!IS_TAURI) {
    console.warn("[localDb] Not running in Tauri environment, using MockDb.");
    _db = MockDb;
    return _db;
  }

  // Use rusqlite-backed Tauri commands instead of tauri-plugin-sql
  const db: Database = {
    execute: async (sql: string, values?: unknown[]): Promise<ExecResult> => {
      const result = await invoke<ExecResult>("db_execute", { sql, values: values ?? [] });
      return result;
    },
    select: async <T>(sql: string, values?: unknown[]): Promise<T> => {
      const result = await invoke<T>("db_select", { sql, values: values ?? [] });
      return result;
    },
    execute_batch: async (sql: string): Promise<void> => {
      await invoke<void>("db_execute_batch", { sql });
    },
    executeTransaction: async (
      statements: DbTransactionStatement[],
    ): Promise<ExecResult[]> => invoke<ExecResult[]>("db_execute_transaction", {
      statements: statements.map((statement) => ({
        sql: statement.sql,
        values: statement.values ?? [],
        expected_rows: statement.expectedRows ?? null,
        error_message: statement.errorMessage ?? null,
      })),
    }),
    load: async (_path: string): Promise<Database> => {
      // Connection is already opened by Rust setup; the path arg is ignored.
      await runMigrations(db);
      await ensureCrrTablesEnabled(db);
      return db;
    },
  };

  try {
    await db.load("");
  } catch (err) {
    console.error("[localDb] Migration error:", err);
    throw err;
  }

  _db = db;
  return _db;
}

// ─────────────────────────────────────────────────────────────────────────────
// SCHEMA MIGRATIONS
// Each migration runs exactly once, tracked by user_version pragma.
// ─────────────────────────────────────────────────────────────────────────────

/** Highest schema version this build knows how to migrate to. Bump this
 * alongside adding a new migrate_vN. */
const MAX_KNOWN_SCHEMA_VERSION = 22;

/**
 * One-time repair for devices whose local DB was left in the specific
 * half-migrated state migrate_v15 could produce before it was wrapped in a
 * transaction: branch_inventory_crr exists (with data) but branch_inventory
 * does not, and user_version is still 14. Without this, such a device would
 * fail migrate_v15 forever on every launch (its INSERT ... FROM
 * branch_inventory errors because that table no longer exists) — the app
 * ships a fix for future migrations but the already-broken ones need this
 * one-time heal. Safe to call unconditionally: it's a no-op unless the
 * exact broken state is detected.
 */
export async function repairIncompleteV15Migration(db: Database, user_version: number): Promise<void> {
  if (user_version >= 15) return;
  const hasOld = await tableExists(db, "branch_inventory");
  const hasStaged = await tableExists(db, "branch_inventory_crr");
  if (!hasOld && hasStaged) {
    console.warn(
      "[localDb] Detected incomplete migrate_v15 (branch_inventory missing, " +
      "branch_inventory_crr present) — repairing before migrations run."
    );
    await db.execute("ALTER TABLE branch_inventory_crr RENAME TO branch_inventory");
  }
}

/**
 * Refuse to proceed if the local DB's schema is newer than this build
 * understands (e.g. a downgrade, or a device that synced its app but not
 * this component). Continuing would run the `ensure*Schema` repair
 * functions — which assume a schema shape this build knows about — against
 * an unknown newer schema, risking silent corruption. Failing loudly here
 * is safer: the caller already treats a migration failure as a blocking
 * sync/init error today.
 */
export async function guardAgainstSchemaDowngrade(_db: Database, user_version: number): Promise<void> {
  if (user_version > MAX_KNOWN_SCHEMA_VERSION) {
    throw new Error(
      `[localDb] Local database schema (v${user_version}) is newer than this app build ` +
      `supports (v${MAX_KNOWN_SCHEMA_VERSION}). Refusing to start to avoid corrupting data — ` +
      "please update the application."
    );
  }
}

async function runMigrations(db: Database): Promise<void> {
  // If MockDb, user_version check will return empty or throw, so we guard.
  try {
      const rows = await db.select<{ user_version: number }[]>(
        "PRAGMA user_version"
      );
      const user_version = rows?.[0]?.user_version ?? 0;

      await repairIncompleteV15Migration(db, user_version);
      await guardAgainstSchemaDowngrade(db, user_version);

      if (user_version < 1) await migrate_v1(db);
      if (user_version < 2) await migrate_v2(db);
      if (user_version < 3) await migrate_v3(db);
      if (user_version < 4) await migrate_v4(db);
      if (user_version < 5) await migrate_v5(db);
      if (user_version < 6) await migrate_v6(db);
      if (user_version < 7) await migrate_v7(db);
      if (user_version < 8) await migrate_v8(db);
      if (user_version < 9) await migrate_v9(db);
      if (user_version < 10) await migrate_v10(db);
      if (user_version < 11) await migrate_v11(db);
      if (user_version < 12) await migrate_v12(db);
      if (user_version < 13) await migrate_v13(db);
      if (user_version < 14) await migrate_v14(db);
      if (user_version < 15) await migrate_v15(db);
      if (user_version < 16) await migrate_v16(db);
      if (user_version < 17) await migrate_v17(db);
      if (user_version < 18) await migrate_v18(db);
      if (user_version < 19) await migrate_v19(db);
      if (user_version < 20) await migrate_v20(db);
      if (user_version < 21) await migrate_v21(db);
      if (user_version < 22) await migrate_v22(db);
      await ensureSuppressedCrrChangesSchema(db);
      await ensureCrrAuditUploadSchema(db);
      await ensureCustomerMergeDirectiveSchema(db);
      await ensureBranchInventorySchema(db);
      await ensureCrrMeta(db);
      await ensureAuditLogSchema(db);
  } catch (e) {
      const msg = (e && typeof e === "object" && "message" in e)
        ? (e as { message: unknown }).message
        : String(e);
      console.error("[localDb] Migration failed:", msg);
      throw e;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V1 — initial schema
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v1(db: Database): Promise<void> {
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
      requires_verification     INTEGER NOT NULL DEFAULT 0,
      requires_approval         INTEGER NOT NULL DEFAULT 0,
      daily_usage_limit         INTEGER,
      per_customer_usage_limit  INTEGER,
      insurance_provider_id     TEXT,
      requires_preauthorization INTEGER NOT NULL DEFAULT 0,
      minimum_purchase_amount   REAL,
      maximum_purchase_amount   REAL,
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

  await db.execute(`
    CREATE TABLE IF NOT EXISTS branch_inventory (
      id                TEXT NOT NULL PRIMARY KEY,
      branch_id         TEXT NOT NULL DEFAULT '',
      drug_id           TEXT NOT NULL DEFAULT '',
      quantity          INTEGER NOT NULL DEFAULT 0,
      reserved_quantity INTEGER NOT NULL DEFAULT 0,
      location          TEXT,
      selling_price     REAL,
      sync_status       TEXT NOT NULL DEFAULT 'synced',
      sync_version      INTEGER NOT NULL DEFAULT 1,
      synced_at         TEXT,
      updated_at        TEXT NOT NULL DEFAULT '',
      created_at        TEXT NOT NULL DEFAULT ''
    )
  `);

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
      subtotal                      REAL NOT NULL,
      discount_amount               REAL NOT NULL DEFAULT 0,
      tax_amount                    REAL NOT NULL DEFAULT 0,
      total_amount                  REAL NOT NULL,
      price_contract_id             TEXT,
      contract_name                 TEXT,
      contract_discount_percentage  REAL,
      payment_method                TEXT NOT NULL DEFAULT 'cash',
      payment_status                TEXT NOT NULL DEFAULT 'completed',
      amount_paid                   REAL,
      change_amount                 REAL NOT NULL DEFAULT 0,
      payment_reference             TEXT,
      prescription_id               TEXT,
      prescription_number           TEXT,
      prescriber_name               TEXT,
      cashier_id                    TEXT NOT NULL,
      pharmacist_id                 TEXT,
      insurance_claim_number        TEXT,
      patient_copay_amount          REAL,
      insurance_covered_amount      REAL,
      insurance_verified            INTEGER NOT NULL DEFAULT 0,
      insurance_verified_at         TEXT,
      insurance_verified_by         TEXT,
      notes                         TEXT,
      status                        TEXT NOT NULL DEFAULT 'completed',
      cancelled_at                  TEXT,
      cancelled_by                  TEXT,
      cancellation_reason           TEXT,
      refund_amount                 REAL,
      refunded_at                   TEXT,
      receipt_printed               INTEGER NOT NULL DEFAULT 0,
      receipt_emailed               INTEGER NOT NULL DEFAULT 0,
      items_json                    TEXT DEFAULT '[]',
      items_count                   INTEGER NOT NULL DEFAULT 0,
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
      conflict_json   TEXT,
      UNIQUE(table_name, record_id)
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS sync_meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
  `);

  await db.execute("PRAGMA user_version = 1");
}

async function migrate_v2(db: Database): Promise<void> {
  const addColumn = async (col: string) => {
    try { await db.execute(`ALTER TABLE sales ADD COLUMN ${col}`); }
    catch { }
  };
  await addColumn("discount_amount               REAL NOT NULL DEFAULT 0");
  await addColumn("prescription_number           TEXT");
  await addColumn("prescriber_name               TEXT");
  await addColumn("receipt_printed               INTEGER NOT NULL DEFAULT 0");
  await addColumn("receipt_emailed               INTEGER NOT NULL DEFAULT 0");
  await db.execute("PRAGMA user_version = 2");
}

async function migrate_v3(db: Database): Promise<void> {
  const addColumn = async (col: string) => {
    try { await db.execute(`ALTER TABLE sales ADD COLUMN ${col}`); }
    catch { }
  };
  await addColumn("discount_amount               REAL NOT NULL DEFAULT 0");
  await addColumn("prescription_number           TEXT");
  await addColumn("prescriber_name               TEXT");
  await addColumn("receipt_printed               INTEGER NOT NULL DEFAULT 0");
  await addColumn("receipt_emailed               INTEGER NOT NULL DEFAULT 0");
  await db.execute("PRAGMA user_version = 3");
}

async function migrate_v4(db: Database): Promise<void> {
  const addCol = async (col: string) => {
    try { await db.execute(`ALTER TABLE drugs ADD COLUMN ${col}`); }
    catch { }
  };
  await addCol("controlled_substance_schedule TEXT");
  await addCol("ndc_code                      TEXT");
  await addCol("markup_percentage             REAL");
  await addCol("max_stock_level               INTEGER");
  await addCol("contraindications             TEXT");
  await db.execute("PRAGMA user_version = 4");
}

async function migrate_v5(db: Database): Promise<void> {
  try {
    await db.execute("ALTER TABLE sync_queue ADD COLUMN conflict_json TEXT");
  } catch { }
  await db.execute("PRAGMA user_version = 5");
}

async function migrate_v6(db: Database): Promise<void> {
  await ensureBranchInventorySchema(db);
  await db.execute("PRAGMA user_version = 6");
}

async function migrate_v7(db: Database): Promise<void> {
  await ensureBranchInventorySchema(db);
  await db.execute(`
    DELETE FROM branch_inventory
    WHERE id LIKE '%:%'
      AND quantity = 0
      AND reserved_quantity = 0
      AND sync_status = 'synced'
  `);
  await db.execute("PRAGMA user_version = 7");
}

async function migrate_v8(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS offline_sales (
      id                    TEXT PRIMARY KEY,
      idempotency_key       TEXT NOT NULL UNIQUE,
      sale_data             TEXT NOT NULL,
      sale_items            TEXT NOT NULL,
      inventory_updates     TEXT NOT NULL,
      recorded_at           TEXT NOT NULL,
      sync_status           TEXT NOT NULL DEFAULT 'pending',
      retry_count           INTEGER NOT NULL DEFAULT 0,
      last_retry_at         TEXT,
      next_retry_at         TEXT,
      error_message         TEXT,
      created_at            TEXT NOT NULL,
      updated_at            TEXT NOT NULL
    )
  `);
  await db.execute("PRAGMA user_version = 8");
}

async function migrate_v9(db: Database): Promise<void> {
  try {
    await db.execute("ALTER TABLE sales ADD COLUMN items_count INTEGER NOT NULL DEFAULT 0");
  } catch { }
  await db.execute("PRAGMA user_version = 9");
}

async function migrate_v10(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS prescriptions (
      id                    TEXT PRIMARY KEY,
      organization_id       TEXT NOT NULL,
      branch_id             TEXT NOT NULL DEFAULT '',
      prescription_number   TEXT NOT NULL UNIQUE,
      customer_id           TEXT NOT NULL,
      prescriber_name       TEXT NOT NULL,
      prescriber_license    TEXT NOT NULL,
      prescriber_phone      TEXT,
      prescriber_address    TEXT,
      issue_date            TEXT NOT NULL,
      expiry_date           TEXT NOT NULL,
      medications           TEXT NOT NULL, -- JSON array
      diagnosis             TEXT,
      notes                 TEXT,
      special_instructions  TEXT,
      refills_allowed       INTEGER NOT NULL DEFAULT 0,
      refills_remaining     INTEGER NOT NULL DEFAULT 0,
      last_refill_date      TEXT,
      status                TEXT NOT NULL DEFAULT 'active',
      verified_by           TEXT,
      verified_at           TEXT,
      created_offline_at    TEXT,
      sync_status           TEXT NOT NULL DEFAULT 'synced',
      sync_version          INTEGER NOT NULL DEFAULT 1,
      synced_at             TEXT,
      updated_at            TEXT NOT NULL,
      created_at            TEXT NOT NULL
    )
  `);
  await db.execute("PRAGMA user_version = 10");
}

async function migrate_v11(db: Database): Promise<void> {
  const addCol = async (col: string) => {
    try { await db.execute(`ALTER TABLE price_contracts ADD COLUMN ${col}`); }
    catch { }
  };
  await addCol("requires_verification     INTEGER NOT NULL DEFAULT 0");
  await addCol("requires_approval         INTEGER NOT NULL DEFAULT 0");
  await addCol("daily_usage_limit         INTEGER");
  await addCol("per_customer_usage_limit  INTEGER");
  await addCol("insurance_provider_id     TEXT");
  await addCol("requires_preauthorization INTEGER NOT NULL DEFAULT 0");
  await addCol("minimum_purchase_amount   REAL");
  await addCol("maximum_purchase_amount   REAL");
  await db.execute("PRAGMA user_version = 11");
}

async function migrate_v12(db: Database): Promise<void> {
  const addQueueColumn = async (column: string) => {
    try { await db.execute(`ALTER TABLE sync_queue ADD COLUMN ${column}`); }
    catch { }
  };

  await addQueueColumn("operation_id TEXT");
  await addQueueColumn("next_attempt_at TEXT");

  // Existing queue rows predate operation-level idempotency. Give every row a
  // stable UUID-shaped identifier so retries after this migration are safe.
  await db.execute(`
    UPDATE sync_queue
    SET operation_id =
      lower(hex(randomblob(4))) || '-' ||
      lower(hex(randomblob(2))) || '-' ||
      lower(hex(randomblob(2))) || '-' ||
      lower(hex(randomblob(2))) || '-' ||
      lower(hex(randomblob(6)))
    WHERE operation_id IS NULL OR operation_id = ''
  `);
  await db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_queue_operation_id ON sync_queue(operation_id)"
  );
  await db.execute(
    "CREATE INDEX IF NOT EXISTS idx_sync_queue_next_attempt ON sync_queue(next_attempt_at)"
  );
  await db.execute("PRAGMA user_version = 12");
}

async function migrate_v13(db: Database): Promise<void> {
  try {
    await db.execute("ALTER TABLE prescriptions ADD COLUMN branch_id TEXT NOT NULL DEFAULT ''");
  } catch {
    // column already exists
  }
  await db.execute("PRAGMA user_version = 13");
}

async function migrate_v14(db: Database): Promise<void> {
  try {
    await db.execute("ALTER TABLE prescriptions ADD COLUMN created_offline_at TEXT");
  } catch {
    // column already exists
  }
  await db.execute("PRAGMA user_version = 14");
}

/// Migration v15: convert branch_inventory to a cr-sqlite CRR table.
/// cr-sqlite v0.16+ requires every NOT NULL non-PK column to have a DEFAULT.
/// Recreate the table with missing defaults, then run crsql_as_crr().
export async function migrate_v15(db: Database): Promise<void> {
  // Wrapped in a transaction — see v16/v17/v22 for the same pattern. Without
  // this, a crash between DROP TABLE branch_inventory and the subsequent
  // RENAME left a device with neither table, and every future launch would
  // fail this same migration again (the retry's INSERT ... FROM
  // branch_inventory fails because the source table no longer exists),
  // permanently bricking that device with no repair path but deleting the
  // local DB. See docs/reviews/2026-08-11-offline-first-architecture-review.md.
  await db.execute("BEGIN IMMEDIATE");
  try {
    // Recreate branch_inventory with DEFAULTs on all NOT NULL non-PK columns.
    // sentinel defaults: zero-text for FK, 0 for numeric
    // NOTE: cr-sqlite v0.16+ does NOT support additional UNIQUE constraints on CRR tables.
    // The UNIQUE(branch_id, drug_id) was removed — deduplication is handled at the
    // application level (server-side CrrSyncService validates FK/ranges).
    await db.execute(`
      CREATE TABLE IF NOT EXISTS branch_inventory_crr (
        id                TEXT NOT NULL PRIMARY KEY,
        branch_id         TEXT NOT NULL DEFAULT '',
        drug_id           TEXT NOT NULL DEFAULT '',
        quantity          INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        location          TEXT,
        selling_price     REAL,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT ''
      )
    `);
    // Deduplicate by (branch_id, drug_id) — keep the row with the latest updated_at
    await db.execute(`
      INSERT INTO branch_inventory_crr
      SELECT id, branch_id, drug_id, quantity, reserved_quantity,
             location, selling_price, sync_status, sync_version, synced_at,
             updated_at, created_at
      FROM (
        SELECT *, ROW_NUMBER() OVER (
          PARTITION BY branch_id, drug_id ORDER BY updated_at DESC
        ) AS rn
        FROM branch_inventory
      )
      WHERE rn = 1
    `);
    await db.execute("DROP TABLE branch_inventory");
    await db.execute("ALTER TABLE branch_inventory_crr RENAME TO branch_inventory");

    // Convert to CRDT replicated row (requires cr-sqlite extension loaded).
    // A failure here is an accepted degraded mode (extension unavailable),
    // not a migration failure — it must NOT roll back the DDL above, so it
    // stays in its own nested try/catch, same as every later CRR migration.
    try {
      await db.execute_batch("SELECT crsql_as_crr('branch_inventory')");
      console.log("[localDb] branch_inventory converted to CRR");
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        ["crr_enabled_branch_inventory", "1"]
      );
    } catch (e) {
      console.warn("[localDb] crsql_as_crr not available — running without CRDT:", e);
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        ["crr_enabled_branch_inventory", "0"]
      );
    }

    await db.execute("PRAGMA user_version = 15");
    await db.execute("COMMIT");
  } catch (e) {
    try { await db.execute("ROLLBACK"); } catch { /* best effort */ }
    throw e;
  }
}

/// Migration v16: convert drug_batches, customers, prescriptions, purchase_orders, sales
/// to cr-sqlite CRR tables. Recreates each table with DEFAULTs on all NOT NULL columns,
/// removes UNIQUE constraints from sale_number and prescription_number.
export async function migrate_v16(db: Database): Promise<void> {
  await db.execute("BEGIN IMMEDIATE");
  try {
  // Helper: recreate a table with new DDL and copy every row.
  async function recreateTable(
    oldName: string,
    newName: string,
    ddl: string,
    selectCols: string,
  ): Promise<void> {
    await db.execute(ddl);
    await db.execute(`
      INSERT INTO ${newName} (${selectCols})
      SELECT ${selectCols} FROM ${oldName}
    `);
    await db.execute(`DROP TABLE ${oldName}`);
    await db.execute(`ALTER TABLE ${newName} RENAME TO ${oldName}`);
  }

  interface BusinessKeyRow {
    id: string;
    scope_value: string;
    business_key: string;
    created_at: string;
  }

  await db.execute(`
    CREATE TABLE IF NOT EXISTS crr_renumber_audit (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id          TEXT NOT NULL UNIQUE,
      table_name        TEXT NOT NULL,
      winner_id         TEXT NOT NULL,
      loser_id          TEXT NOT NULL,
      business_key_col  TEXT NOT NULL,
      old_business_key  TEXT NOT NULL,
      new_business_key  TEXT NOT NULL,
      renumbered_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      uploaded_at       TEXT
    )
  `);

  /**
   * Preserve all document/transaction rows while removing a business-key UNIQUE
   * constraint. The earliest-created row keeps the original key; later rows are
   * assigned the same -B ... -Z suffixes as the server keep_both_renumber path.
   */
  async function copyKeepingBoth(
    oldName: string,
    newName: string,
    ddl: string,
    selectCols: string,
    scopeColumn: string,
    businessKeyColumn: string,
  ): Promise<void> {
    await db.execute(ddl);

    const rows = await db.select<BusinessKeyRow[]>(`
      SELECT id,
             ${scopeColumn} AS scope_value,
             ${businessKeyColumn} AS business_key,
             created_at
      FROM ${oldName}
      ORDER BY ${scopeColumn}, ${businessKeyColumn}, created_at, id
    `);

    const usedByScope = new Map<string, Set<string>>();
    for (const row of rows) {
      const used = usedByScope.get(row.scope_value) ?? new Set<string>();
      used.add(row.business_key);
      usedByScope.set(row.scope_value, used);
    }

    const winnerByCollision = new Map<string, string>();
    for (const row of rows) {
      const collisionKey = JSON.stringify([row.scope_value, row.business_key]);
      const winnerId = winnerByCollision.get(collisionKey);
      if (winnerId === undefined) {
        winnerByCollision.set(collisionKey, row.id);
        continue;
      }

      const used = usedByScope.get(row.scope_value)!;
      let newBusinessKey: string | null = null;
      for (const suffix of "BCDEFGHIJKLMNOPQRSTUVWXYZ") {
        const baseBusinessKey = /-[A-Z]$/.test(row.business_key)
          ? row.business_key.slice(0, -2)
          : row.business_key;
        const candidate = `${baseBusinessKey}-${suffix}`;
        if (!used.has(candidate)) {
          newBusinessKey = candidate;
          used.add(candidate);
          break;
        }
      }
      if (newBusinessKey === null) {
        throw new Error(
          `Cannot preserve ${oldName} row ${row.id}: no keep_both_renumber suffix remains for ${row.business_key}`
        );
      }

      await db.execute(
        `UPDATE ${oldName} SET ${businessKeyColumn} = $1 WHERE id = $2`,
        [newBusinessKey, row.id],
      );
      await db.execute(
        `INSERT INTO crr_renumber_audit
           (event_id, table_name, winner_id, loser_id, business_key_col,
            old_business_key, new_business_key)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT(event_id) DO NOTHING`,
        [`${oldName}:${row.id}:${row.business_key}:${newBusinessKey}`,
         oldName, winnerId, row.id, businessKeyColumn,
         row.business_key, newBusinessKey],
      );
    }

    await db.execute(`
      INSERT INTO ${newName} (${selectCols})
      SELECT ${selectCols} FROM ${oldName}
    `);
    await db.execute(`DROP TABLE ${oldName}`);
    await db.execute(`ALTER TABLE ${newName} RENAME TO ${oldName}`);
  }

  // ── drug_batches ──────────────────────────────────────────────────
  // Add DEFAULTs, no UNIQUE to remove
  await recreateTable(
    "drug_batches", "drug_batches_crr",
    `CREATE TABLE drug_batches_crr (
      id                  TEXT NOT NULL PRIMARY KEY,
      branch_id           TEXT NOT NULL DEFAULT '',
      drug_id             TEXT NOT NULL DEFAULT '',
      batch_number        TEXT NOT NULL DEFAULT '',
      quantity            INTEGER NOT NULL DEFAULT 0,
      remaining_quantity  INTEGER NOT NULL DEFAULT 0,
      manufacturing_date  TEXT,
      expiry_date         TEXT NOT NULL DEFAULT '',
      cost_price          REAL,
      selling_price       REAL,
      supplier            TEXT,
      purchase_order_id   TEXT,
      sync_status         TEXT NOT NULL DEFAULT 'synced',
      sync_version        INTEGER NOT NULL DEFAULT 1,
      synced_at           TEXT,
      updated_at          TEXT NOT NULL DEFAULT '',
      created_at          TEXT NOT NULL DEFAULT ''
    )`,
    `id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
     manufacturing_date, expiry_date, cost_price, selling_price, supplier,
     purchase_order_id, sync_status, sync_version, synced_at, updated_at, created_at`,
  );

  // ── customers ─────────────────────────────────────────────────────
  // Add DEFAULTs, no UNIQUE to remove
  await recreateTable(
    "customers", "customers_crr",
    `CREATE TABLE customers_crr (
      id                      TEXT NOT NULL PRIMARY KEY,
      organization_id         TEXT NOT NULL DEFAULT '',
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
      updated_at              TEXT NOT NULL DEFAULT '',
      created_at              TEXT NOT NULL DEFAULT ''
    )`,
    `id, organization_id, customer_type, first_name, last_name, phone, email,
     date_of_birth, loyalty_points, loyalty_tier, insurance_provider_id,
     insurance_member_id, preferred_contract_id, is_active, is_deleted,
     sync_status, sync_version, synced_at, updated_at, created_at`,
  );

  // ── prescriptions ─────────────────────────────────────────────────
  // Remove UNIQUE on prescription_number, add DEFAULTs
  const prescCols = `id, organization_id, branch_id, prescription_number, customer_id,
    prescriber_name, prescriber_license, prescriber_phone, prescriber_address,
    issue_date, expiry_date, medications, diagnosis, notes, special_instructions,
    refills_allowed, refills_remaining, last_refill_date, status, verified_by,
    verified_at, sync_status, sync_version, synced_at, updated_at, created_at`;

  await copyKeepingBoth(
    "prescriptions", "prescriptions_crr",
    `CREATE TABLE prescriptions_crr (
    id                    TEXT NOT NULL PRIMARY KEY,
    organization_id       TEXT NOT NULL DEFAULT '',
    branch_id             TEXT NOT NULL DEFAULT '',
    prescription_number   TEXT NOT NULL DEFAULT '',
    customer_id           TEXT NOT NULL DEFAULT '',
    prescriber_name       TEXT NOT NULL DEFAULT '',
    prescriber_license    TEXT NOT NULL DEFAULT '',
    prescriber_phone      TEXT,
    prescriber_address    TEXT,
    issue_date            TEXT NOT NULL DEFAULT '',
    expiry_date           TEXT NOT NULL DEFAULT '',
    medications           TEXT NOT NULL DEFAULT '[]',
    diagnosis             TEXT,
    notes                 TEXT,
    special_instructions  TEXT,
    refills_allowed       INTEGER NOT NULL DEFAULT 0,
    refills_remaining     INTEGER NOT NULL DEFAULT 0,
    last_refill_date      TEXT,
    status                TEXT NOT NULL DEFAULT 'active',
    verified_by           TEXT,
    verified_at           TEXT,
    sync_status           TEXT NOT NULL DEFAULT 'synced',
    sync_version          INTEGER NOT NULL DEFAULT 1,
    synced_at             TEXT,
    updated_at            TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT ''
    )`,
    prescCols,
    "organization_id",
    "prescription_number",
  );

  // ── purchase_orders ───────────────────────────────────────────────
  // Add DEFAULTs, no UNIQUE to remove (client-side)
  await recreateTable(
    "purchase_orders", "purchase_orders_crr",
    `CREATE TABLE purchase_orders_crr (
      id                    TEXT NOT NULL PRIMARY KEY,
      organization_id       TEXT NOT NULL DEFAULT '',
      branch_id             TEXT NOT NULL DEFAULT '',
      po_number             TEXT NOT NULL DEFAULT '',
      supplier_id           TEXT NOT NULL DEFAULT '',
      subtotal              REAL NOT NULL DEFAULT 0,
      tax_amount            REAL NOT NULL DEFAULT 0,
      shipping_cost         REAL NOT NULL DEFAULT 0,
      total_amount          REAL NOT NULL DEFAULT 0,
      status                TEXT NOT NULL DEFAULT 'draft',
      ordered_by            TEXT NOT NULL DEFAULT '',
      approved_by           TEXT,
      approved_at           TEXT,
      expected_delivery_date TEXT,
      received_date         TEXT,
      notes                 TEXT,
      items_json            TEXT NOT NULL DEFAULT '[]',
      sync_status           TEXT NOT NULL DEFAULT 'synced',
      sync_version          INTEGER NOT NULL DEFAULT 1,
      synced_at             TEXT,
      updated_at            TEXT NOT NULL DEFAULT '',
      created_at            TEXT NOT NULL DEFAULT ''
    )`,
    `id, organization_id, branch_id, po_number, supplier_id, subtotal, tax_amount,
     shipping_cost, total_amount, status, ordered_by, approved_by, approved_at,
     expected_delivery_date, received_date, notes, items_json, sync_status,
     sync_version, synced_at, updated_at, created_at`,
  );

  // ── sales ─────────────────────────────────────────────────────────
  // Remove UNIQUE on sale_number, add DEFAULTs
  // Ensure columns that v1's sales table doesn't have exist before SELECT
  const addSaleCol = async (col: string) => {
    try { await db.execute(`ALTER TABLE sales ADD COLUMN ${col}`); } catch { }
  };
  await addSaleCol("contract_type TEXT");
  await addSaleCol("split_payment_details TEXT");
  await addSaleCol("insurance_preauth_number TEXT");
  await addSaleCol("prescriber_license TEXT");
  await addSaleCol("refunded_by TEXT");
  await addSaleCol("refund_reason TEXT");
  await addSaleCol("refund_reference TEXT");
  const saleCols = `id, organization_id, branch_id, sale_number, customer_id,
    customer_name, subtotal, discount_amount, tax_amount, total_amount,
    price_contract_id, contract_name, contract_discount_percentage, contract_type,
    payment_method, payment_status, amount_paid, change_amount, payment_reference,
    split_payment_details, insurance_preauth_number, prescription_id,
    prescription_number, prescriber_name, prescriber_license, cashier_id,
    pharmacist_id, insurance_claim_number, patient_copay_amount,
    insurance_covered_amount, insurance_verified, insurance_verified_at,
    insurance_verified_by, notes, status, cancelled_at, cancelled_by,
    cancellation_reason, refund_amount, refunded_at, refunded_by, refund_reason,
    refund_reference, receipt_printed, receipt_emailed, items_json, items_count,
    sync_status, sync_version, synced_at, updated_at, created_at`;

  await copyKeepingBoth(
    "sales", "sales_crr",
    `CREATE TABLE sales_crr (
    id                            TEXT NOT NULL PRIMARY KEY,
    organization_id               TEXT NOT NULL DEFAULT '',
    branch_id                     TEXT NOT NULL DEFAULT '',
    sale_number                   TEXT NOT NULL DEFAULT '',
    customer_id                   TEXT,
    customer_name                 TEXT,
    subtotal                      REAL NOT NULL DEFAULT 0,
    discount_amount               REAL NOT NULL DEFAULT 0,
    tax_amount                    REAL NOT NULL DEFAULT 0,
    total_amount                  REAL NOT NULL DEFAULT 0,
    price_contract_id             TEXT,
    contract_name                 TEXT,
    contract_discount_percentage  REAL,
    contract_type                 TEXT,
    payment_method                TEXT NOT NULL DEFAULT 'cash',
    payment_status                TEXT NOT NULL DEFAULT 'completed',
    amount_paid                   REAL,
    change_amount                 REAL NOT NULL DEFAULT 0,
    payment_reference             TEXT,
    split_payment_details         TEXT,
    insurance_preauth_number      TEXT,
    prescription_id               TEXT,
    prescription_number           TEXT,
    prescriber_name               TEXT,
    prescriber_license            TEXT,
    cashier_id                    TEXT NOT NULL DEFAULT '',
    pharmacist_id                 TEXT,
    insurance_claim_number        TEXT,
    patient_copay_amount          REAL,
    insurance_covered_amount      REAL,
    insurance_verified            INTEGER NOT NULL DEFAULT 0,
    insurance_verified_at         TEXT,
    insurance_verified_by         TEXT,
    notes                         TEXT,
    status                        TEXT NOT NULL DEFAULT 'completed',
    cancelled_at                  TEXT,
    cancelled_by                  TEXT,
    cancellation_reason           TEXT,
    refund_amount                 REAL,
    refunded_at                   TEXT,
    refunded_by                   TEXT,
    refund_reason                 TEXT,
    refund_reference              TEXT,
    receipt_printed               INTEGER NOT NULL DEFAULT 0,
    receipt_emailed               INTEGER NOT NULL DEFAULT 0,
    items_json                    TEXT NOT NULL DEFAULT '[]',
    items_count                   INTEGER NOT NULL DEFAULT 0,
    sync_status                   TEXT NOT NULL DEFAULT 'synced',
    sync_version                  INTEGER NOT NULL DEFAULT 1,
    synced_at                     TEXT,
    updated_at                    TEXT NOT NULL DEFAULT '',
    created_at                    TEXT NOT NULL DEFAULT ''
    )`,
    saleCols,
    "branch_id",
    "sale_number",
  );

  // ── Convert all 5 tables to CRR ───────────────────────────────────
  const crrTables = [
    "drug_batches", "customers", "prescriptions",
    "purchase_orders", "sales",
  ];
  for (const table of crrTables) {
    try {
      await db.execute_batch(`SELECT crsql_as_crr('${table}')`);
      console.log(`[localDb] ${table} converted to CRR`);
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        [`crr_enabled_${table}`, "1"]
      );
    } catch (e) {
      console.warn(`[localDb] crsql_as_crr for ${table} not available:`, e);
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        [`crr_enabled_${table}`, "0"]
      );
    }
  }

    await db.execute("COMMIT");
    await db.execute("PRAGMA user_version = 16");
  } catch (error) {
    try {
      await db.execute("ROLLBACK");
    } catch {
      // Preserve the original migration failure if rollback also fails.
    }
    throw error;
  }
}

/// Migration v17: convert remaining legacy-pulled reference/audit tables to CRR.
/// These are server-authored in practice, but CRR transport now carries their
/// deltas so the desktop sync loop does not need the legacy /sync/pull path.
async function migrate_v17(db: Database): Promise<void> {
  await db.execute("BEGIN IMMEDIATE");
  try {
    try {
      await db.execute("ALTER TABLE drugs ADD COLUMN image_url TEXT");
    } catch { }

    async function recreateTable(
      oldName: string,
      newName: string,
      ddl: string,
      selectCols: string,
    ): Promise<void> {
      await db.execute(ddl);
      await db.execute(`
        INSERT INTO ${newName} (${selectCols})
        SELECT ${selectCols} FROM ${oldName}
      `);
      await db.execute(`DROP TABLE ${oldName}`);
      await db.execute(`ALTER TABLE ${newName} RENAME TO ${oldName}`);
    }

    await recreateTable(
      "drugs", "drugs_crr",
      `CREATE TABLE drugs_crr (
        id                TEXT NOT NULL PRIMARY KEY,
        organization_id   TEXT NOT NULL DEFAULT '',
        name              TEXT NOT NULL DEFAULT '',
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
        requires_prescription INTEGER NOT NULL DEFAULT 0,
        controlled_substance_schedule TEXT,
        ndc_code          TEXT,
        unit_price        REAL NOT NULL DEFAULT 0,
        cost_price        REAL,
        markup_percentage REAL,
        tax_rate          REAL NOT NULL DEFAULT 0,
        reorder_level     INTEGER NOT NULL DEFAULT 10,
        reorder_quantity  INTEGER NOT NULL DEFAULT 50,
        max_stock_level   INTEGER,
        unit_of_measure   TEXT NOT NULL DEFAULT 'unit',
        description       TEXT,
        usage_instructions TEXT,
        side_effects      TEXT,
        contraindications TEXT,
        storage_conditions TEXT,
        image_url         TEXT,
        is_active         INTEGER NOT NULL DEFAULT 1,
        is_deleted        INTEGER NOT NULL DEFAULT 0,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT ''
      )`,
      `id, organization_id, name, generic_name, brand_name, sku, barcode,
       category_id, drug_type, dosage_form, strength, manufacturer, supplier,
       requires_prescription, controlled_substance_schedule, ndc_code,
       unit_price, cost_price, markup_percentage, tax_rate, reorder_level,
       reorder_quantity, max_stock_level, unit_of_measure, description,
       usage_instructions, side_effects, contraindications, storage_conditions,
       image_url, is_active, is_deleted, sync_status, sync_version, synced_at,
       updated_at, created_at`,
    );

    await recreateTable(
      "drug_categories", "drug_categories_crr",
      `CREATE TABLE drug_categories_crr (
        id              TEXT NOT NULL PRIMARY KEY,
        organization_id TEXT NOT NULL DEFAULT '',
        name            TEXT NOT NULL DEFAULT '',
        description     TEXT,
        parent_id       TEXT,
        path            TEXT,
        level           INTEGER NOT NULL DEFAULT 0,
        is_deleted      INTEGER NOT NULL DEFAULT 0,
        sync_status     TEXT NOT NULL DEFAULT 'synced',
        sync_version    INTEGER NOT NULL DEFAULT 1,
        synced_at       TEXT,
        updated_at      TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT ''
      )`,
      `id, organization_id, name, description, parent_id, path, level,
       is_deleted, sync_status, sync_version, synced_at, updated_at, created_at`,
    );

    await recreateTable(
      "price_contracts", "price_contracts_crr",
      `CREATE TABLE price_contracts_crr (
        id                        TEXT NOT NULL PRIMARY KEY,
        organization_id           TEXT NOT NULL DEFAULT '',
        contract_code             TEXT NOT NULL DEFAULT '',
        contract_name             TEXT NOT NULL DEFAULT '',
        contract_type             TEXT NOT NULL DEFAULT 'standard',
        is_default_contract       INTEGER NOT NULL DEFAULT 0,
        discount_type             TEXT NOT NULL DEFAULT 'percentage',
        discount_percentage       REAL NOT NULL DEFAULT 0,
        applies_to_prescription_only INTEGER NOT NULL DEFAULT 0,
        applies_to_otc            INTEGER NOT NULL DEFAULT 1,
        applies_to_all_branches   INTEGER NOT NULL DEFAULT 1,
        applicable_branch_ids     TEXT NOT NULL DEFAULT '[]',
        effective_from            TEXT NOT NULL DEFAULT '',
        effective_to              TEXT,
        requires_verification     INTEGER NOT NULL DEFAULT 0,
        requires_approval         INTEGER NOT NULL DEFAULT 0,
        daily_usage_limit         INTEGER,
        per_customer_usage_limit  INTEGER,
        insurance_provider_id     TEXT,
        requires_preauthorization INTEGER NOT NULL DEFAULT 0,
        minimum_purchase_amount   REAL,
        maximum_purchase_amount   REAL,
        status                    TEXT NOT NULL DEFAULT 'active',
        is_active                 INTEGER NOT NULL DEFAULT 1,
        copay_amount              REAL,
        copay_percentage          REAL,
        is_deleted                INTEGER NOT NULL DEFAULT 0,
        sync_status               TEXT NOT NULL DEFAULT 'synced',
        sync_version              INTEGER NOT NULL DEFAULT 1,
        synced_at                 TEXT,
        updated_at                TEXT NOT NULL DEFAULT '',
        created_at                TEXT NOT NULL DEFAULT ''
      )`,
      `id, organization_id, contract_code, contract_name, contract_type,
       is_default_contract, discount_type, discount_percentage,
       applies_to_prescription_only, applies_to_otc, applies_to_all_branches,
       applicable_branch_ids, effective_from, effective_to,
       requires_verification, requires_approval, daily_usage_limit,
       per_customer_usage_limit, insurance_provider_id,
       requires_preauthorization, minimum_purchase_amount,
       maximum_purchase_amount, status, is_active, copay_amount,
       copay_percentage, is_deleted, sync_status, sync_version, synced_at,
       updated_at, created_at`,
    );

    await recreateTable(
      "audit_logs", "audit_logs_crr",
      `CREATE TABLE audit_logs_crr (
        id                TEXT NOT NULL PRIMARY KEY,
        organization_id   TEXT NOT NULL DEFAULT '',
        user_id           TEXT,
        user_full_name    TEXT,
        action            TEXT NOT NULL DEFAULT '',
        entity_type       TEXT,
        entity_id         TEXT,
        changes           TEXT,
        ip_address        TEXT,
        user_agent        TEXT,
        context_metadata  TEXT,
        created_at        TEXT NOT NULL DEFAULT '',
        updated_at        TEXT NOT NULL DEFAULT '',
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        last_synced_at    TEXT,
        sync_hash         TEXT
      )`,
      `id, organization_id, user_id, user_full_name, action, entity_type,
       entity_id, changes, ip_address, user_agent, context_metadata, created_at,
       updated_at, sync_status, sync_version, last_synced_at, sync_hash`,
    );

    for (const table of ["drugs", "drug_categories", "price_contracts", "audit_logs"]) {
      try {
        await db.execute_batch(`SELECT crsql_as_crr('${table}')`);
        await db.execute(
          "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
          [`crr_enabled_${table}`, "1"]
        );
      } catch (e) {
        console.warn(`[localDb] crsql_as_crr for ${table} not available:`, e);
        await db.execute(
          "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
          [`crr_enabled_${table}`, "0"]
        );
      }
    }

    await db.execute("COMMIT");
    await db.execute("PRAGMA user_version = 17");
  } catch (error) {
    try {
      await db.execute("ROLLBACK");
    } catch {}
    throw error;
  }
}

async function migrate_v18(db: Database): Promise<void> {
  // v1.2.38 and older decoded pulled CRR BLOB fields into Uint8Array values,
  // but the Tauri SQLite bridge stored those arrays as JSON text. Affected
  // devices may have advanced this cursor without actually merging remote rows.
  // Reset only the pull cursor; the independent push cursor remains unchanged.
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
    ["crr_pull_db_version", "0"],
  );
  await db.execute("PRAGMA user_version = 18");
}

export async function migrate_v19(db: Database): Promise<void> {
  // Sales are commands with inventory, batch, prescription, and ledger side
  // effects. They must use the receipt-backed protocol-v2 queue, never the
  // generic CRR row merge path.
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
    ["crr_enabled_sales", "0"],
  );
  await db.execute("PRAGMA user_version = 19");
}

export async function migrate_v20(db: Database): Promise<void> {
  try {
    await db.execute(
      "ALTER TABLE offline_sales ADD COLUMN crr_start_db_version INTEGER NOT NULL DEFAULT 0",
    );
  } catch {
    // The column can already exist after an interrupted migration retry.
  }
  await ensureSuppressedCrrChangesSchema(db);
  await db.execute("PRAGMA user_version = 20");
}

/// Migration v21: version-forwarding stub — the comprehensive CRR table rebuild
/// was moved to v22 to ensure it always runs even if v21 was partially applied.
async function migrate_v21(_db: Database): Promise<void> {
  await _db.execute("PRAGMA user_version = 21");
}

export async function ensureSuppressedCrrChangesSchema(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS suppressed_crr_changes (
      table_name  TEXT NOT NULL,
      db_version  INTEGER NOT NULL,
      sale_id     TEXT NOT NULL,
      reason      TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (table_name, db_version, sale_id)
    )
  `);
  await db.execute(
    "CREATE INDEX IF NOT EXISTS idx_suppressed_crr_version ON suppressed_crr_changes(table_name, db_version)",
  );
}
// ─────────────────────────────────────────────────────────────────────────────
// CRR TABLE SCHEMA HELPERS (used by migrate_v19)
// ─────────────────────────────────────────────────────────────────────────────

export async function tableExists(db: Database, name: string): Promise<boolean> {
  const rows = await db.select<{ name: string }[]>(
    "SELECT name FROM sqlite_master WHERE type='table' AND name=$1", [name]
  );
  return rows.length > 0;
}


/// Migration v22: unconditionally rebuild every CRR table with an explicit NOT
/// NULL primary key, remove any forbidden UNIQUE indexes, and clear tracking
/// flags so ensureCrrTablesEnabled starts fresh.
async function migrate_v22(db: Database): Promise<void> {
  await db.execute("BEGIN IMMEDIATE");
  try {
    const crrTableDefs: Array<{ table: string; ddl: string; cols: string }> = [];

    // Collect table definitions for all CRR tables that exist.
    // Each definition uses TEXT NOT NULL PRIMARY KEY and DEFAULTs on all NOT
    // NULL non-PK columns, with no UNIQUE constraints.
    // NOTE: we do NOT pre-drop unique indexes — inline UNIQUE constraints
    // create auto-indexes that cannot be dropped independently. The table
    // rebuild naturally drops them since the new DDL omits UNIQUE.
    for (const table of CRR_TABLES) {
      if (!(await tableExists(db, table))) continue;

      // Build the corrected DDL and column list from a map so we don't repeat
      // the long literal blocks for every table.
      const defs: Record<string, { ddl: string; cols: string }> = {
        branch_inventory: {
          ddl: `CREATE TABLE branch_inventory_v22 (
            id                TEXT NOT NULL PRIMARY KEY,
            branch_id         TEXT NOT NULL DEFAULT '',
            drug_id           TEXT NOT NULL DEFAULT '',
            quantity          INTEGER NOT NULL DEFAULT 0,
            reserved_quantity INTEGER NOT NULL DEFAULT 0,
            location          TEXT,
            selling_price     REAL,
            sync_status       TEXT NOT NULL DEFAULT 'synced',
            sync_version      INTEGER NOT NULL DEFAULT 1,
            synced_at         TEXT,
            updated_at        TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, branch_id, drug_id, quantity, reserved_quantity, location,
            selling_price, sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        drug_batches: {
          ddl: `CREATE TABLE drug_batches_v22 (
            id                  TEXT NOT NULL PRIMARY KEY,
            branch_id           TEXT NOT NULL DEFAULT '',
            drug_id             TEXT NOT NULL DEFAULT '',
            batch_number        TEXT NOT NULL DEFAULT '',
            quantity            INTEGER NOT NULL DEFAULT 0,
            remaining_quantity  INTEGER NOT NULL DEFAULT 0,
            manufacturing_date  TEXT,
            expiry_date         TEXT NOT NULL DEFAULT '',
            cost_price          REAL,
            selling_price       REAL,
            supplier            TEXT,
            purchase_order_id   TEXT,
            sync_status         TEXT NOT NULL DEFAULT 'synced',
            sync_version        INTEGER NOT NULL DEFAULT 1,
            synced_at           TEXT,
            updated_at          TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
            manufacturing_date, expiry_date, cost_price, selling_price, supplier,
            purchase_order_id, sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        customers: {
          ddl: `CREATE TABLE customers_v22 (
            id                      TEXT NOT NULL PRIMARY KEY,
            organization_id         TEXT NOT NULL DEFAULT '',
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
            updated_at              TEXT NOT NULL DEFAULT '',
            created_at              TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, customer_type, first_name, last_name, phone, email,
            date_of_birth, loyalty_points, loyalty_tier, insurance_provider_id,
            insurance_member_id, preferred_contract_id, is_active, is_deleted,
            sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        prescriptions: {
          ddl: `CREATE TABLE prescriptions_v22 (
            id                    TEXT NOT NULL PRIMARY KEY,
            organization_id       TEXT NOT NULL DEFAULT '',
            branch_id             TEXT NOT NULL DEFAULT '',
            prescription_number   TEXT NOT NULL DEFAULT '',
            customer_id           TEXT NOT NULL DEFAULT '',
            prescriber_name       TEXT NOT NULL DEFAULT '',
            prescriber_license    TEXT NOT NULL DEFAULT '',
            prescriber_phone      TEXT,
            prescriber_address    TEXT,
            issue_date            TEXT NOT NULL DEFAULT '',
            expiry_date           TEXT NOT NULL DEFAULT '',
            medications           TEXT NOT NULL DEFAULT '[]',
            diagnosis             TEXT,
            notes                 TEXT,
            special_instructions  TEXT,
            refills_allowed       INTEGER NOT NULL DEFAULT 0,
            refills_remaining     INTEGER NOT NULL DEFAULT 0,
            last_refill_date      TEXT,
            status                TEXT NOT NULL DEFAULT 'active',
            verified_by           TEXT,
            verified_at           TEXT,
            sync_status           TEXT NOT NULL DEFAULT 'synced',
            sync_version          INTEGER NOT NULL DEFAULT 1,
            synced_at             TEXT,
            updated_at            TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, branch_id, prescription_number, customer_id,
            prescriber_name, prescriber_license, prescriber_phone, prescriber_address,
            issue_date, expiry_date, medications, diagnosis, notes, special_instructions,
            refills_allowed, refills_remaining, last_refill_date, status, verified_by,
            verified_at, sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        purchase_orders: {
          ddl: `CREATE TABLE purchase_orders_v22 (
            id                    TEXT NOT NULL PRIMARY KEY,
            organization_id       TEXT NOT NULL DEFAULT '',
            branch_id             TEXT NOT NULL DEFAULT '',
            po_number             TEXT NOT NULL DEFAULT '',
            supplier_id           TEXT NOT NULL DEFAULT '',
            subtotal              REAL NOT NULL DEFAULT 0,
            tax_amount            REAL NOT NULL DEFAULT 0,
            shipping_cost         REAL NOT NULL DEFAULT 0,
            total_amount          REAL NOT NULL DEFAULT 0,
            status                TEXT NOT NULL DEFAULT 'draft',
            ordered_by            TEXT NOT NULL DEFAULT '',
            approved_by           TEXT,
            approved_at           TEXT,
            expected_delivery_date TEXT,
            received_date         TEXT,
            notes                 TEXT,
            items_json            TEXT NOT NULL DEFAULT '[]',
            sync_status           TEXT NOT NULL DEFAULT 'synced',
            sync_version          INTEGER NOT NULL DEFAULT 1,
            synced_at             TEXT,
            updated_at            TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, branch_id, po_number, supplier_id, subtotal, tax_amount,
            shipping_cost, total_amount, status, ordered_by, approved_by, approved_at,
            expected_delivery_date, received_date, notes, items_json, sync_status,
            sync_version, synced_at, updated_at, created_at`,
        },
        sales: {
          ddl: `CREATE TABLE sales_v22 (
            id                            TEXT NOT NULL PRIMARY KEY,
            organization_id               TEXT NOT NULL DEFAULT '',
            branch_id                     TEXT NOT NULL DEFAULT '',
            sale_number                   TEXT NOT NULL DEFAULT '',
            customer_id                   TEXT,
            customer_name                 TEXT,
            subtotal                      REAL NOT NULL DEFAULT 0,
            discount_amount               REAL NOT NULL DEFAULT 0,
            tax_amount                    REAL NOT NULL DEFAULT 0,
            total_amount                  REAL NOT NULL DEFAULT 0,
            price_contract_id             TEXT,
            contract_name                 TEXT,
            contract_discount_percentage  REAL,
            contract_type                 TEXT,
            payment_method                TEXT NOT NULL DEFAULT 'cash',
            payment_status                TEXT NOT NULL DEFAULT 'completed',
            amount_paid                   REAL,
            change_amount                 REAL NOT NULL DEFAULT 0,
            payment_reference             TEXT,
            split_payment_details         TEXT,
            insurance_preauth_number      TEXT,
            prescription_id               TEXT,
            prescription_number           TEXT,
            prescriber_name               TEXT,
            prescriber_license            TEXT,
            cashier_id                    TEXT NOT NULL DEFAULT '',
            pharmacist_id                 TEXT,
            insurance_claim_number        TEXT,
            patient_copay_amount          REAL,
            insurance_covered_amount      REAL,
            insurance_verified            INTEGER NOT NULL DEFAULT 0,
            insurance_verified_at         TEXT,
            insurance_verified_by         TEXT,
            notes                         TEXT,
            status                        TEXT NOT NULL DEFAULT 'completed',
            cancelled_at                  TEXT,
            cancelled_by                  TEXT,
            cancellation_reason           TEXT,
            refund_amount                 REAL,
            refunded_at                   TEXT,
            refunded_by                   TEXT,
            refund_reason                 TEXT,
            refund_reference              TEXT,
            receipt_printed               INTEGER NOT NULL DEFAULT 0,
            receipt_emailed               INTEGER NOT NULL DEFAULT 0,
            items_json                    TEXT NOT NULL DEFAULT '[]',
            items_count                   INTEGER NOT NULL DEFAULT 0,
            sync_status                   TEXT NOT NULL DEFAULT 'synced',
            sync_version                  INTEGER NOT NULL DEFAULT 1,
            synced_at                     TEXT,
            updated_at                    TEXT NOT NULL DEFAULT '',
            created_at                    TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, branch_id, sale_number, customer_id,
            customer_name, subtotal, discount_amount, tax_amount, total_amount,
            price_contract_id, contract_name, contract_discount_percentage,
            contract_type, payment_method, payment_status, amount_paid,
            change_amount, payment_reference, split_payment_details,
            insurance_preauth_number, prescription_id, prescription_number,
            prescriber_name, prescriber_license, cashier_id, pharmacist_id,
            insurance_claim_number, patient_copay_amount, insurance_covered_amount,
            insurance_verified, insurance_verified_at, insurance_verified_by,
            notes, status, cancelled_at, cancelled_by, cancellation_reason,
            refund_amount, refunded_at, refunded_by, refund_reason,
            refund_reference, receipt_printed, receipt_emailed, items_json,
            items_count, sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        drugs: {
          ddl: `CREATE TABLE drugs_v22 (
            id                TEXT NOT NULL PRIMARY KEY,
            organization_id   TEXT NOT NULL DEFAULT '',
            name              TEXT NOT NULL DEFAULT '',
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
            requires_prescription INTEGER NOT NULL DEFAULT 0,
            controlled_substance_schedule TEXT,
            ndc_code          TEXT,
            unit_price        REAL NOT NULL DEFAULT 0,
            cost_price        REAL,
            markup_percentage REAL,
            tax_rate          REAL NOT NULL DEFAULT 0,
            reorder_level     INTEGER NOT NULL DEFAULT 10,
            reorder_quantity  INTEGER NOT NULL DEFAULT 50,
            max_stock_level   INTEGER,
            unit_of_measure   TEXT NOT NULL DEFAULT 'unit',
            description       TEXT,
            usage_instructions TEXT,
            side_effects      TEXT,
            contraindications TEXT,
            storage_conditions TEXT,
            image_url         TEXT,
            is_active         INTEGER NOT NULL DEFAULT 1,
            is_deleted        INTEGER NOT NULL DEFAULT 0,
            sync_status       TEXT NOT NULL DEFAULT 'synced',
            sync_version      INTEGER NOT NULL DEFAULT 1,
            synced_at         TEXT,
            updated_at        TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, name, generic_name, brand_name, sku, barcode,
            category_id, drug_type, dosage_form, strength, manufacturer, supplier,
            requires_prescription, controlled_substance_schedule, ndc_code,
            unit_price, cost_price, markup_percentage, tax_rate, reorder_level,
            reorder_quantity, max_stock_level, unit_of_measure, description,
            usage_instructions, side_effects, contraindications, storage_conditions,
            image_url, is_active, is_deleted, sync_status, sync_version, synced_at,
            updated_at, created_at`,
        },
        drug_categories: {
          ddl: `CREATE TABLE drug_categories_v22 (
            id              TEXT NOT NULL PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT '',
            name            TEXT NOT NULL DEFAULT '',
            description     TEXT,
            parent_id       TEXT,
            path            TEXT,
            level           INTEGER NOT NULL DEFAULT 0,
            is_deleted      INTEGER NOT NULL DEFAULT 0,
            sync_status     TEXT NOT NULL DEFAULT 'synced',
            sync_version    INTEGER NOT NULL DEFAULT 1,
            synced_at       TEXT,
            updated_at      TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, name, description, parent_id, path, level,
            is_deleted, sync_status, sync_version, synced_at, updated_at, created_at`,
        },
        price_contracts: {
          ddl: `CREATE TABLE price_contracts_v22 (
            id                        TEXT NOT NULL PRIMARY KEY,
            organization_id           TEXT NOT NULL DEFAULT '',
            contract_code             TEXT NOT NULL DEFAULT '',
            contract_name             TEXT NOT NULL DEFAULT '',
            contract_type             TEXT NOT NULL DEFAULT 'standard',
            is_default_contract       INTEGER NOT NULL DEFAULT 0,
            discount_type             TEXT NOT NULL DEFAULT 'percentage',
            discount_percentage       REAL NOT NULL DEFAULT 0,
            applies_to_prescription_only INTEGER NOT NULL DEFAULT 0,
            applies_to_otc            INTEGER NOT NULL DEFAULT 1,
            applies_to_all_branches   INTEGER NOT NULL DEFAULT 1,
            applicable_branch_ids     TEXT NOT NULL DEFAULT '[]',
            effective_from            TEXT NOT NULL DEFAULT '',
            effective_to              TEXT,
            requires_verification     INTEGER NOT NULL DEFAULT 0,
            requires_approval         INTEGER NOT NULL DEFAULT 0,
            daily_usage_limit         INTEGER,
            per_customer_usage_limit  INTEGER,
            insurance_provider_id     TEXT,
            requires_preauthorization INTEGER NOT NULL DEFAULT 0,
            minimum_purchase_amount   REAL,
            maximum_purchase_amount   REAL,
            status                    TEXT NOT NULL DEFAULT 'active',
            is_active                 INTEGER NOT NULL DEFAULT 1,
            copay_amount              REAL,
            copay_percentage          REAL,
            is_deleted                INTEGER NOT NULL DEFAULT 0,
            sync_status               TEXT NOT NULL DEFAULT 'synced',
            sync_version              INTEGER NOT NULL DEFAULT 1,
            synced_at                 TEXT,
            updated_at                TEXT NOT NULL DEFAULT '',
            created_at                TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, organization_id, contract_code, contract_name, contract_type,
            is_default_contract, discount_type, discount_percentage,
            applies_to_prescription_only, applies_to_otc, applies_to_all_branches,
            applicable_branch_ids, effective_from, effective_to,
            requires_verification, requires_approval, daily_usage_limit,
            per_customer_usage_limit, insurance_provider_id,
            requires_preauthorization, minimum_purchase_amount,
            maximum_purchase_amount, status, is_active, copay_amount,
            copay_percentage, is_deleted, sync_status, sync_version, synced_at,
            updated_at, created_at`,
        },
        audit_logs: {
          ddl: `CREATE TABLE audit_logs_v22 (
            id                TEXT NOT NULL PRIMARY KEY,
            organization_id   TEXT NOT NULL DEFAULT '',
            user_id           TEXT,
            user_full_name    TEXT,
            action            TEXT NOT NULL DEFAULT '',
            entity_type       TEXT,
            entity_id         TEXT,
            changes           TEXT,
            ip_address        TEXT,
            user_agent        TEXT,
            context_metadata  TEXT,
            created_at        TEXT NOT NULL DEFAULT '',
            updated_at        TEXT NOT NULL DEFAULT '',
            sync_status       TEXT NOT NULL DEFAULT 'synced',
            sync_version      INTEGER NOT NULL DEFAULT 1,
            last_synced_at    TEXT,
            sync_hash         TEXT
          )`,
          cols: `id, organization_id, user_id, user_full_name, action, entity_type,
            entity_id, changes, ip_address, user_agent, context_metadata, created_at,
            updated_at, sync_status, sync_version, last_synced_at, sync_hash`,
        },
      };

      const def = defs[table];
      if (!def) continue;
      crrTableDefs.push({ table, ...def });
    }

    // Rebuild every collected table
    for (const { table, ddl, cols } of crrTableDefs) {
      const tmpName = `${table}_v22`;
      await db.execute(`DROP TABLE IF EXISTS ${tmpName}`);
      await db.execute(ddl);
      await db.execute(
        `INSERT INTO ${tmpName} (${cols}) SELECT ${cols} FROM ${table}`
      );
      await db.execute(`DROP TABLE ${table}`);
      await db.execute(`ALTER TABLE ${tmpName} RENAME TO ${table}`);
    }

    // Clear CRR tracking flags so ensureCrrTablesEnabled re-detects from scratch
    for (const table of CRR_TABLES) {
      await db.execute(
        "DELETE FROM sync_meta WHERE key = $1",
        [`crr_enabled_${table}`]
      );
    }
    resetCrrCache();

    await db.execute("PRAGMA user_version = 22");
    await db.execute("COMMIT");
    console.log("[localDb] Migration v22 complete — CRR tables rebuilt with NOT NULL PKs");
  } catch (error) {
    try { await db.execute("ROLLBACK"); } catch { }
    console.error("[localDb] Migration v22 failed:", error);
    throw error;
  }
}

async function ensureCrrAuditUploadSchema(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS crr_renumber_audit (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id          TEXT NOT NULL UNIQUE,
      table_name        TEXT NOT NULL,
      winner_id         TEXT NOT NULL,
      loser_id          TEXT NOT NULL,
      business_key_col  TEXT NOT NULL,
      old_business_key  TEXT NOT NULL,
      new_business_key  TEXT NOT NULL,
      renumbered_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      uploaded_at       TEXT
    )
  `);
  try {
    await db.execute("ALTER TABLE crr_renumber_audit ADD COLUMN event_id TEXT");
  } catch { }
  try {
    await db.execute("ALTER TABLE crr_renumber_audit ADD COLUMN uploaded_at TEXT");
  } catch { }
  await db.execute(`
    UPDATE crr_renumber_audit
    SET event_id = table_name || ':' || loser_id || ':' ||
                   old_business_key || ':' || new_business_key
    WHERE event_id IS NULL OR event_id = ''
  `);
  await db.execute(`
    CREATE UNIQUE INDEX IF NOT EXISTS uq_crr_renumber_audit_event_id
    ON crr_renumber_audit(event_id)
  `);
}

async function ensureCustomerMergeDirectiveSchema(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS customer_merge_aliases (
      loser_id     TEXT PRIMARY KEY,
      survivor_id  TEXT NOT NULL,
      event_id     TEXT NOT NULL,
      merged_at    TEXT NOT NULL
    )
  `);
  await db.execute(`
    CREATE TABLE IF NOT EXISTS applied_customer_merge_directives (
      event_id    TEXT PRIMARY KEY,
      applied_at  TEXT NOT NULL
    )
  `);
}

async function ensureBranchInventorySchema(db: Database): Promise<void> {
  try {
    await db.execute("ALTER TABLE branch_inventory ADD COLUMN selling_price REAL");
  } catch { }
}

const KNOWN_CRR_TABLES = [
  "drugs",
  "drug_categories",
  "price_contracts",
  "audit_logs",
  "branch_inventory",
  "drug_batches",
  "customers",
  "prescriptions",
  "purchase_orders",
  "sales",
];

export async function ensureCrrMeta(db: Database): Promise<void> {
  for (const table of KNOWN_CRR_TABLES) {
    const exists = await db.select<{ name: string }[]>(
      "SELECT name FROM sqlite_master WHERE type='table' AND name=$1",
      [table]
    );
    if (exists.length === 0) continue;

    const rows = await db.select<{ key: string }[]>(
      "SELECT key FROM sync_meta WHERE key = $1",
      [`crr_enabled_${table}`]
    );
    if (rows.length === 0) {
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        [`crr_enabled_${table}`, "0"]
      );
    }
  }
}

export async function ensureAuditLogSchema(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS audit_logs (
      id                TEXT NOT NULL PRIMARY KEY,
      organization_id   TEXT NOT NULL,
      user_id           TEXT,
      user_full_name    TEXT,
      action            TEXT NOT NULL,
      entity_type       TEXT,
      entity_id         TEXT,
      changes           TEXT,
      ip_address        TEXT,
      user_agent        TEXT,
      context_metadata  TEXT,
      created_at        TEXT NOT NULL DEFAULT '',
      updated_at        TEXT NOT NULL DEFAULT '',
      sync_status       TEXT NOT NULL DEFAULT 'synced',
      sync_version      INTEGER NOT NULL DEFAULT 1,
      last_synced_at    TEXT,
      sync_hash         TEXT
    )
  `);
  // CREATE TABLE IF NOT EXISTS does not evolve databases created by older
  // desktop releases. Add the display-only server field idempotently so a
  // legacy pull cannot abort before advancing its cursor and reaching CRR pull.
  try {
    await db.execute("ALTER TABLE audit_logs ADD COLUMN user_full_name TEXT");
  } catch {
    // Duplicate-column is expected on every startup after the first repair.
  }
}

interface LocalTableColumn {
  name: string;
  notnull?: number;
  dflt_value?: unknown;
  pk?: number;
}

async function getLocalTableColumns(
  db: Database,
  table: string,
): Promise<LocalTableColumn[]> {
  return db.select<LocalTableColumn[]>(`PRAGMA table_info(${table})`);
}

function needsCrrDefaultRepair(columns: LocalTableColumn[]): boolean {
  return columns.some((column) => {
    if (column.pk && column.pk > 0) return false;
    return column.notnull === 1 && column.dflt_value == null;
  });
}

async function recreateTableForCrr(
  db: Database,
  oldName: string,
  newName: string,
  ddl: string,
  selectCols: string,
): Promise<void> {
  await db.execute(ddl);
  await db.execute(`
    INSERT INTO ${newName} (${selectCols})
    SELECT ${selectCols} FROM ${oldName}
  `);
  await db.execute(`DROP TABLE ${oldName}`);
  await db.execute(`ALTER TABLE ${newName} RENAME TO ${oldName}`);
}

async function ensureDrugsCrrCompatibleSchema(db: Database): Promise<boolean> {
  const exists = await db.select<{ name: string }[]>(
    "SELECT name FROM sqlite_master WHERE type='table' AND name=$1",
    ["drugs"]
  );
  if (exists.length === 0) return false;

  try {
    await db.execute("ALTER TABLE drugs ADD COLUMN image_url TEXT");
  } catch {
    // Already present on databases that reached migration v17 cleanly.
  }

  const columns = await getLocalTableColumns(db, "drugs");
  if (!needsCrrDefaultRepair(columns)) return false;

  await recreateTableForCrr(
    db,
    "drugs",
    "drugs_crr_repair",
    `CREATE TABLE drugs_crr_repair (
      id                TEXT NOT NULL PRIMARY KEY,
      organization_id   TEXT NOT NULL DEFAULT '',
      name              TEXT NOT NULL DEFAULT '',
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
      requires_prescription INTEGER NOT NULL DEFAULT 0,
      controlled_substance_schedule TEXT,
      ndc_code          TEXT,
      unit_price        REAL NOT NULL DEFAULT 0,
      cost_price        REAL,
      markup_percentage REAL,
      tax_rate          REAL NOT NULL DEFAULT 0,
      reorder_level     INTEGER NOT NULL DEFAULT 10,
      reorder_quantity  INTEGER NOT NULL DEFAULT 50,
      max_stock_level   INTEGER,
      unit_of_measure   TEXT NOT NULL DEFAULT 'unit',
      description       TEXT,
      usage_instructions TEXT,
      side_effects      TEXT,
      contraindications TEXT,
      storage_conditions TEXT,
      image_url         TEXT,
      is_active         INTEGER NOT NULL DEFAULT 1,
      is_deleted        INTEGER NOT NULL DEFAULT 0,
      sync_status       TEXT NOT NULL DEFAULT 'synced',
      sync_version      INTEGER NOT NULL DEFAULT 1,
      synced_at         TEXT,
      updated_at        TEXT NOT NULL DEFAULT '',
      created_at        TEXT NOT NULL DEFAULT ''
    )`,
    `id, organization_id, name, generic_name, brand_name, sku, barcode,
     category_id, drug_type, dosage_form, strength, manufacturer, supplier,
     requires_prescription, controlled_substance_schedule, ndc_code,
     unit_price, cost_price, markup_percentage, tax_rate, reorder_level,
     reorder_quantity, max_stock_level, unit_of_measure, description,
     usage_instructions, side_effects, contraindications, storage_conditions,
     image_url, is_active, is_deleted, sync_status, sync_version, synced_at,
     updated_at, created_at`,
  );
  return true;
}

// ─────────────────────────────────────────────────────────────────────────────

const CRR_TABLES: readonly string[] = [
  "branch_inventory",
  "drug_batches",
  "customers",
  "prescriptions",
  "purchase_orders",
  "drugs",
  "drug_categories",
  "price_contracts",
  "audit_logs",
];

async function hasCrrChangeTracking(db: Database, table: string): Promise<boolean> {
  try {
    const rows = await db.select<{ count: number }[]>(
      "SELECT COUNT(*) as count FROM crsql_master WHERE tbl_name = $1",
      [table],
    );
    if ((rows[0]?.count ?? 0) > 0) return true;
  } catch {
    // Older broken installs can have cr-sqlite unavailable or partially loaded.
  }

  try {
    const triggers = await db.select<{ count: number }[]>(
      `SELECT COUNT(*) as count
       FROM sqlite_master
       WHERE type = 'trigger'
         AND tbl_name = $1
         AND name LIKE '%crsql%'`,
      [table],
    );
    return (triggers[0]?.count ?? 0) > 0;
  } catch {
    return false;
  }
}

async function hasCrrRuntime(db: Database): Promise<boolean> {
  try {
    await db.select<unknown[]>("SELECT 1 FROM crsql_changes LIMIT 1");
    return true;
  } catch {
    return false;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    for (const key of ["message", "error", "details"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim() !== "") return value;
    }
    try {
      return JSON.stringify(error);
    } catch {
      return Object.prototype.toString.call(error);
    }
  }
  return String(error);
}

/** Repair missing CRR triggers and reset the pull cursor if any table was missing. */
export async function ensureCrrTablesEnabled(
  db: Database,
  options: { strict?: boolean } = { strict: IS_TAURI },
): Promise<void> {
  let repaired = false;
  const failures: string[] = [];

  for (const table of CRR_TABLES) {
    const rows = await db.select<{ value: string }[]>(
      "SELECT value FROM sync_meta WHERE key = $1",
      [`crr_enabled_${table}`]
    );

    const exists = await db.select<{ name: string }[]>(
      "SELECT name FROM sqlite_master WHERE type='table' AND name=$1",
      [table]
    );
    if (exists.length === 0) continue;

    if (await hasCrrChangeTracking(db, table)) {
      if (rows[0]?.value !== "1") {
        await db.execute(
          "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
          [`crr_enabled_${table}`, "1"]
        );
        repaired = true;
      }
      continue;
    }

    try {
      if (table === "drugs") {
        const schemaRepaired = await ensureDrugsCrrCompatibleSchema(db);
        if (schemaRepaired) repaired = true;
      }
      await db.execute_batch(`SELECT crsql_as_crr('${table}')`);
      if (!(await hasCrrChangeTracking(db, table)) && !(await hasCrrRuntime(db))) {
        throw new Error("crsql_as_crr completed but crsql_changes is unavailable");
      }
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        [`crr_enabled_${table}`, "1"]
      );
      console.log(`[localDb] CRR enabled for ${table}`);
      repaired = true;
    } catch (e) {
      const message = errorMessage(e);
      failures.push(`${table}: ${message}`);
      console.error(`[localDb] CRR required for ${table} but unavailable:`, e);
      await db.execute(
        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
        [`crr_enabled_${table}`, "0"]
      );
    }
  }

  if (repaired) {
    console.log("[localDb] CRR repair applied — resetting pull cursor");
    await db.execute("DELETE FROM sync_meta WHERE key = 'crr_pull_db_version'");
  }
  resetCrrCache();

  if (failures.length > 0 && options.strict !== false) {
    throw new Error(
      "CR-SQLite change tracking is required for offline sync but could not be enabled: "
      + failures.join("; ")
    );
  }
}

function syncMetaKey(table?: string, branchId?: string): string {
  if (branchId) {
    return table ? `last_sync_at:${branchId}:${table}` : `last_sync_at:${branchId}`;
  }
  return table ? `last_sync_at:${table}` : "last_sync_at";
}

export async function getLastSyncAt(table?: string, branchId?: string): Promise<string | null> {
  const db = await getDb();
  const key = syncMetaKey(table, branchId);
  const rows = await db.select<{ value: string }[]>(
    "SELECT value FROM sync_meta WHERE key = $1",
    [key]
  );
  return rows?.[0]?.value ?? null;
}

export async function setLastSyncAt(
  timestamp: string,
  table?: string,
  branchId?: string,
): Promise<void> {
  const db = await getDb();
  const key = syncMetaKey(table, branchId);
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
  operation_id: string;
  table_name: string;
  record_id: string;
  operation: "create" | "update" | "delete";
  sync_version: number;
  payload_json: string;
  created_offline_at: string;
  attempts: number;
  last_attempt_at: string | null;
  next_attempt_at: string | null;
  error: string | null;
  conflict_json?: string | null;
}

export interface QueueScope {
  organizationId?: string | null;
  branchId?: string | null;
}

export interface QueuedConflict extends QueuedRecord {
  conflict: PushConflict;
  local_data: Record<string, unknown>;
}

export interface QueuedFailure extends QueuedRecord {
  is_blocked: boolean;
  local_data: Record<string, unknown>;
}

const BRANCH_SCOPED_QUEUE_TABLES = new Set([
  "branch_inventory",
  "drug_batches",
  "stock_adjustments",
  "sales",
  "purchase_orders",
  "prescriptions",
]);

const ORGANIZATION_SCOPED_QUEUE_TABLES = new Set([
  "customers",
  "drugs",
  "drug_categories",
  "price_contracts",
]);

let _crrCache: Record<string, boolean> | null = null;

export async function isCrrTable(table: string): Promise<boolean> {
  // A sale is a domain command, not a mergeable row. Its server-side effects
  // are applied by SyncService._push_sale under an operation receipt.
  if (table === "sales") return false;
  if (_crrCache === null) {
    const db = await getDb();
    const rows = await db.select<{ key: string; value: string }[]>(
      "SELECT key, value FROM sync_meta WHERE key LIKE 'crr_enabled_%'"
    );
    _crrCache = {};
    for (const row of rows) {
      _crrCache[row.key.replace("crr_enabled_", "")] = row.value === "1";
    }
  }
  return _crrCache[table] ?? false;
}

export function resetCrrCache(): void {
  _crrCache = null;
}

export const SYNC_QUEUE_CHANGED_EVENT = "laso:sync-queue-changed";

export function notifySyncQueueChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(SYNC_QUEUE_CHANGED_EVENT));
}

function hasQueueScope(scope?: QueueScope): boolean {
  return Boolean(scope?.organizationId || scope?.branchId);
}

function asScopeString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function parseQueuePayload(row: QueuedRecord): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(row.payload_json);
    return parsed && typeof parsed === "object"
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

export function isQueuedRecordInScope(row: QueuedRecord, scope?: QueueScope): boolean {
  if (!hasQueueScope(scope)) return true;

  const payload = parseQueuePayload(row);
  if (!payload) return false;

  const branchId = asScopeString(payload.branch_id);
  const organizationId = asScopeString(payload.organization_id);

  if (BRANCH_SCOPED_QUEUE_TABLES.has(row.table_name)) {
    return Boolean(scope?.branchId && branchId === scope.branchId);
  }

  if (ORGANIZATION_SCOPED_QUEUE_TABLES.has(row.table_name)) {
    return Boolean(scope?.organizationId && organizationId === scope.organizationId);
  }

  if (branchId) {
    return Boolean(scope?.branchId && branchId === scope.branchId);
  }

  if (organizationId) {
    return Boolean(scope?.organizationId && organizationId === scope.organizationId);
  }

  return false;
}

function filterQueueByScope<T extends QueuedRecord>(rows: T[], scope?: QueueScope): T[] {
  if (!hasQueueScope(scope)) return rows;
  return rows.filter((row) => isQueuedRecordInScope(row, scope));
}

export async function enqueue(
  tableName: string,
  recordId: string,
  operation: "create" | "update" | "delete",
  syncVersion: number,
  payload: Record<string, unknown>,
  operationIdOverride?: string,
): Promise<void> {
  // CRR tables are tracked by cr-sqlite's crsql_changes — skip the old sync_queue
  if (await isCrrTable(tableName)) return;
  const db = await getDb();
  const operationId = operationIdOverride ?? crypto.randomUUID();
  await db.execute(
    `INSERT INTO sync_queue
       (operation_id, table_name, record_id, operation, sync_version, payload_json,
        created_offline_at, next_attempt_at, conflict_json)
     VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, NULL)
     ON CONFLICT(table_name, record_id) DO UPDATE SET
       operation_id = excluded.operation_id,
       operation    = excluded.operation,
       sync_version = excluded.sync_version,
       payload_json = excluded.payload_json,
       attempts     = 0,
       last_attempt_at = NULL,
       next_attempt_at = NULL,
       error        = NULL,
       conflict_json = NULL`,
    [
      operationId,
      tableName,
      recordId,
      operation,
      syncVersion,
      JSON.stringify(payload),
      new Date().toISOString(),
    ]
  );
  notifySyncQueueChanged();
}

export async function getPendingQueue(
  limit = 500,
  maxAttempts = 10,
  scope?: QueueScope
): Promise<QueuedRecord[]> {
  const db = await getDb();
  if (!hasQueueScope(scope)) {
    return db.select<QueuedRecord[]>(
      `SELECT * FROM sync_queue
       WHERE conflict_json IS NULL
         AND attempts < $2
         AND (next_attempt_at IS NULL OR next_attempt_at <= $3)
       ORDER BY id ASC
       LIMIT $1`,
      [limit, maxAttempts, new Date().toISOString()]
    );
  }

  const rows = await db.select<QueuedRecord[]>(
    `SELECT * FROM sync_queue
     WHERE conflict_json IS NULL
       AND attempts < $1
       AND (next_attempt_at IS NULL OR next_attempt_at <= $2)
     ORDER BY id ASC`,
    [maxAttempts, new Date().toISOString()]
  );
  return filterQueueByScope(rows ?? [], scope).slice(0, limit);
}

export async function dequeue(tableName: string, recordId: string): Promise<void> {
  const db = await getDb();
  await db.execute(
    "DELETE FROM sync_queue WHERE table_name = $1 AND record_id = $2",
    [tableName, recordId]
  );
  notifySyncQueueChanged();
}

export async function markQueueError(
  tableName: string,
  recordId: string,
  error: string,
  skipIncrement = false
): Promise<number> {
  const db = await getDb();
  const currentRows = await db.select<{ attempts: number }[]>(
    "SELECT attempts FROM sync_queue WHERE table_name = $1 AND record_id = $2",
    [tableName, recordId]
  );
  const currentAttempts = currentRows[0]?.attempts ?? 0;
  const nextAttemptAt = new Date(
    Date.now() + new RetryBackoff().getDelay(currentAttempts)
  ).toISOString();

  if (skipIncrement) {
    await db.execute(
      `UPDATE sync_queue
       SET last_attempt_at = $1,
           next_attempt_at = $2,
           error = $3
       WHERE table_name = $4 AND record_id = $5`,
      [new Date().toISOString(), nextAttemptAt, error, tableName, recordId]
    );
  } else {
    await db.execute(
      `UPDATE sync_queue
       SET attempts = attempts + 1,
           last_attempt_at = $1,
           next_attempt_at = $2,
           error = $3
       WHERE table_name = $4 AND record_id = $5`,
      [new Date().toISOString(), nextAttemptAt, error, tableName, recordId]
    );
  }

  notifySyncQueueChanged();
  const rows = await db.select<{ attempts: number }[]>(
    "SELECT attempts FROM sync_queue WHERE table_name = $1 AND record_id = $2",
    [tableName, recordId]
  );
  return rows[0]?.attempts ?? 0;
}

export async function markQueueConflict(
  tableName: string,
  recordId: string,
  error: string,
  conflict: PushConflict
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `UPDATE sync_queue
     SET attempts = attempts + 1,
         last_attempt_at = $1,
         error = $2,
         conflict_json = $3
     WHERE table_name = $4 AND record_id = $5`,
    [new Date().toISOString(), error, JSON.stringify(conflict), tableName, recordId]
  );
  notifySyncQueueChanged();
}

export async function clearQueueConflict(
  tableName: string,
  recordId: string
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `UPDATE sync_queue
     SET error = NULL,
         conflict_json = NULL,
         next_attempt_at = NULL
     WHERE table_name = $1 AND record_id = $2`,
    [tableName, recordId]
  );
  notifySyncQueueChanged();
}

export async function getPendingConflicts(scope?: QueueScope): Promise<QueuedConflict[]> {
  const db = await getDb();
  const rows = await db.select<QueuedRecord[] & { conflict_json: string }[]>(
    "SELECT * FROM sync_queue WHERE conflict_json IS NOT NULL ORDER BY id ASC"
  );

  return filterQueueByScope(rows ?? [], scope).map((row) => {
    let conflict: PushConflict = {
      local_id: row.record_id,
      table_name: row.table_name,
      local_version: row.sync_version,
      server_version: 0,
      server_record: {},
      resolution: "manual_required",
    };
    try {
      conflict = JSON.parse(row.conflict_json ?? "{}");
    } catch { }
    return {
      ...row,
      conflict,
      local_data: JSON.parse(row.payload_json),
    };
  });
}

export async function getPendingFailures(
  maxAttempts = 10,
  scope?: QueueScope
): Promise<QueuedFailure[]> {
  const db = await getDb();
  const rows = await db.select<QueuedRecord[]>(
    `SELECT *
     FROM sync_queue
     WHERE conflict_json IS NULL
       AND error IS NOT NULL
     ORDER BY
       CASE WHEN attempts >= $1 THEN 0 ELSE 1 END,
       last_attempt_at DESC,
       id ASC`,
    [maxAttempts]
  );

  return filterQueueByScope(rows ?? [], scope).map((row) => {
    let localData: Record<string, unknown> = {};
    try {
      localData = JSON.parse(row.payload_json);
    } catch { }
    return {
      ...row,
      is_blocked: row.attempts >= maxAttempts,
      local_data: localData,
    };
  });
}

export async function resetPendingFailures(scope?: QueueScope): Promise<void> {
  const db = await getDb();
  if (hasQueueScope(scope)) {
    const rows = await db.select<QueuedRecord[]>(
      `SELECT *
       FROM sync_queue
       WHERE conflict_json IS NULL
         AND error IS NOT NULL
       ORDER BY id ASC`
    );
    const scopedRows = filterQueueByScope(rows ?? [], scope);
    for (const row of scopedRows) {
      await db.execute(
        `UPDATE sync_queue
         SET attempts = 0,
             last_attempt_at = NULL,
             next_attempt_at = NULL,
             error = NULL
         WHERE id = $1`,
        [row.id]
      );
    }
    notifySyncQueueChanged();
    return;
  }

  await db.execute(
    `UPDATE sync_queue
     SET attempts = 0,
         last_attempt_at = NULL,
         next_attempt_at = NULL,
         error = NULL
     WHERE conflict_json IS NULL
       AND error IS NOT NULL`
  );
  notifySyncQueueChanged();
}

export async function getNextRetryAt(
  scope?: QueueScope,
  maxAttempts = 10
): Promise<string | null> {
  const db = await getDb();
  const rows = await db.select<QueuedRecord[]>(
    `SELECT *
     FROM sync_queue
     WHERE conflict_json IS NULL
       AND attempts < $1
       AND next_attempt_at IS NOT NULL
     ORDER BY next_attempt_at ASC`,
    [maxAttempts]
  );
  const scopedRows = filterQueueByScope(rows ?? [], scope);
  return scopedRows[0]?.next_attempt_at ?? null;
}

export async function requeueConflictForLocalWin(
  conflict: QueuedConflict
): Promise<void> {
  const db = await getDb();
  const nextVersion = Math.max(
    conflict.sync_version,
    conflict.conflict.server_version
  ) + 1;
  const payload = {
    ...conflict.local_data,
    sync_version: nextVersion,
    _force_sync_overwrite: true,
  };

  await db.execute(
    `UPDATE sync_queue
     SET operation_id = $1,
         sync_version = $2,
         payload_json = $3,
         attempts = 0,
         last_attempt_at = NULL,
         next_attempt_at = NULL,
         error = NULL,
         conflict_json = NULL
     WHERE table_name = $4 AND record_id = $5`,
    [
      crypto.randomUUID(),
      nextVersion,
      JSON.stringify(payload),
      conflict.table_name,
      conflict.record_id,
    ]
  );
  notifySyncQueueChanged();

  // Manual conflicts currently apply only to locally editable cached tables.
  // Keep the row version aligned with the queue so the next pull cannot
  // overwrite the user's chosen local value before the forced push completes.
  if (conflict.table_name === "customers" || conflict.table_name === "prescriptions") {
    await db.execute(
      `UPDATE ${conflict.table_name}
       SET sync_status = 'pending', sync_version = $1
       WHERE id = $2`,
      [nextVersion, conflict.record_id]
    );
  }
}

export async function getPendingCount(scope?: QueueScope): Promise<number> {
  const db = await getDb();
  if (hasQueueScope(scope)) {
    const rows = await db.select<QueuedRecord[]>(
      "SELECT * FROM sync_queue ORDER BY id ASC"
    );
    return filterQueueByScope(rows ?? [], scope).length;
  }

  const rows = await db.select<{ count: number }[]>(
    "SELECT COUNT(*) as count FROM sync_queue"
  );
  return rows?.[0]?.count ?? 0;
}

// ─── CRR (cr-sqlite) helpers ───────────────────────────────────────────

export interface CrrRenumberAuditEvent {
  event_id: string;
  table_name: "prescriptions" | "purchase_orders" | "sales";
  winner_id: string;
  loser_id: string;
  business_key_col: string;
  old_business_key: string;
  new_business_key: string;
  renumbered_at: string;
}

export async function getPendingCrrRenumberAudits(): Promise<CrrRenumberAuditEvent[]> {
  const db = await getDb();
  return db.select<CrrRenumberAuditEvent[]>(`
    SELECT event_id, table_name, winner_id, loser_id, business_key_col,
           old_business_key, new_business_key, renumbered_at
    FROM crr_renumber_audit
    WHERE uploaded_at IS NULL
    ORDER BY id
    LIMIT 500
  `);
}

export async function markCrrRenumberAuditsUploaded(eventIds: string[]): Promise<void> {
  if (eventIds.length === 0) return;
  const db = await getDb();
  const uploadedAt = new Date().toISOString();
  for (const eventId of eventIds) {
    await db.execute(
      "UPDATE crr_renumber_audit SET uploaded_at = $1 WHERE event_id = $2",
      [uploadedAt, eventId],
    );
  }
}

let _crr_site_id: string | null = null;

/** Get the local cr-sqlite site ID (cached after first call). */
export async function getCrrSiteId(): Promise<string> {
  if (_crr_site_id) return _crr_site_id;
  const db = await getDb();
  const rows = await db.select<{ "crsql_site_id()": string }[]>(
    "SELECT crsql_site_id()"
  );
  _crr_site_id = rows?.[0]?.["crsql_site_id()"] ?? "";
  return _crr_site_id;
}

export interface CrrChangeRow {
  table: string;
  pk: unknown;
  cid: string;
  val: unknown;
  col_version: number;
  db_version: number;
  site_id: string;
  cl: number;
  seq: number;
}

export interface CustomerMergeDirective {
  directive_version: number;
  event_id: string;
  survivor_id: string;
  loser_id: string;
  merged_at: string;
}

async function normalizeMergedCustomerReferences(db: Database): Promise<void> {
  for (const table of ["sales", "prescriptions"]) {
    await db.execute(`
      UPDATE ${table}
      SET customer_id = (
        SELECT survivor_id FROM customer_merge_aliases
        WHERE loser_id = ${table}.customer_id
      )
      WHERE customer_id IN (SELECT loser_id FROM customer_merge_aliases)
    `);
  }
}

export async function applyCustomerMergeDirectives(
  directives: CustomerMergeDirective[],
): Promise<void> {
  if (directives.length === 0) return;
  const db = await getDb();
  await applyCustomerMergeDirectivesToDb(db, directives);
}

export async function applyCustomerMergeDirectivesToDb(
  db: Database,
  directives: CustomerMergeDirective[],
): Promise<void> {
  await db.execute("BEGIN IMMEDIATE");
  try {
    for (const directive of directives) {
      const applied = await db.select<{ event_id: string }[]>(
        "SELECT event_id FROM applied_customer_merge_directives WHERE event_id = $1",
        [directive.event_id],
      );
      if (applied.length > 0) continue;

      // Collapse older aliases if their survivor is itself merged later.
      await db.execute(
        "UPDATE customer_merge_aliases SET survivor_id = $1 WHERE survivor_id = $2",
        [directive.survivor_id, directive.loser_id],
      );
      await db.execute(`
        INSERT INTO customer_merge_aliases
          (loser_id, survivor_id, event_id, merged_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT(loser_id) DO UPDATE SET
          survivor_id = excluded.survivor_id,
          event_id = excluded.event_id,
          merged_at = excluded.merged_at
      `, [
        directive.loser_id,
        directive.survivor_id,
        directive.event_id,
        directive.merged_at,
      ]);

      await normalizeMergedCustomerReferences(db);
      await db.execute("DELETE FROM customers WHERE id = $1", [directive.loser_id]);
      await db.execute(
        "INSERT INTO applied_customer_merge_directives(event_id, applied_at) VALUES($1, $2)",
        [directive.event_id, new Date().toISOString()],
      );
    }
    await normalizeMergedCustomerReferences(db);
    await db.execute("COMMIT");
  } catch (error) {
    try { await db.execute("ROLLBACK"); } catch { }
    throw error;
  }
}

/**
 * Return crsql_changes entries newer than `sinceDbVersion` for the given
 * site_id (defaults to local site).  These are the changes this client
 * produced locally and needs to push to the server.
 */
export async function getCrrPushChanges(
  sinceDbVersion = 0,
): Promise<CrrChangeRow[]> {
  const db = await getDb();
  const siteId = await getCrrSiteId();
  if (!siteId) return [];
  return getCrrPushChangesFromDb(db, siteId, sinceDbVersion);
}

export async function getCrrPushChangesFromDb(
  db: Pick<Database, "execute" | "select">,
  siteId: string,
  sinceDbVersion = 0,
): Promise<CrrChangeRow[]> {
  await ensureSuppressedCrrChangesSchema(db as Database);
  // `seq` resets to 0 at the start of every transaction in cr-sqlite — it
  // orders rows WITHIN one commit, not across commits. Sorting by `seq`
  // alone leaves rows from different transactions that happen to share a
  // `seq` value in an undefined relative order, which can send a
  // dependent row (e.g. a prescription) to the server before the row it
  // references (its customer), permanently failing FK validation and
  // wedging the push cursor forever (see the bug report this fixes: an
  // offline customer + prescription created moments apart never synced).
  // `db_version` is the true chronological ordering across transactions.
  return db.select<CrrChangeRow[]>(
    `SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq
     FROM crsql_changes
     WHERE site_id = ? AND db_version > ? AND "table" <> 'sales'
       AND NOT EXISTS (
         SELECT 1 FROM suppressed_crr_changes suppressed
         WHERE suppressed.table_name = crsql_changes."table"
           AND suppressed.db_version = crsql_changes.db_version
       )
     ORDER BY db_version, seq`,
    [siteId, sinceDbVersion]
  );
}

/**
 * Insert changes from the server into local crsql_changes, triggering
 * cr-sqlite's auto-merge into the local table.
 */
const BLOB_TRANSPORT_KEY = "__laso_blob_b64";

function blobTransportValue(encoded: string): Record<typeof BLOB_TRANSPORT_KEY, string> {
  return { [BLOB_TRANSPORT_KEY]: encoded.slice(4) };
}

function decodeCrrValue(v: unknown): unknown {
  if (typeof v === "string" && v.startsWith("b64:")) {
    return blobTransportValue(v);
  }
  return v;
}

export async function applyCrrPullChanges(
  changes: CrrChangeRow[],
): Promise<void> {
  if (changes.length === 0) return;
  const db = await getDb();
  await applyCrrPullChangesToDb(db, changes);
}

export async function applyCrrPullChangesToDb(
  db: Database,
  changes: CrrChangeRow[],
): Promise<void> {
  const mergeableChanges = changes.filter((change) => change.table !== "sales");
  if (mergeableChanges.length === 0) {
    await normalizeMergedCustomerReferences(db);
    return;
  }
  const tables = new Set(mergeableChanges.map((change) => change.table));
  if ([...tables].some((table) => CRR_TABLES.includes(table))) {
    await ensureCrrTablesEnabled(db);
  }
  await db.execute("BEGIN IMMEDIATE");
  try {
    for (const ch of mergeableChanges) {
      await db.execute(
        `INSERT INTO crsql_changes ("table", pk, cid, val, col_version, db_version, site_id, cl, seq)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
        [
          ch.table,
          decodeCrrValue(ch.pk),
          ch.cid,
          decodeCrrValue(ch.val),
          ch.col_version,
          ch.db_version,
          decodeCrrValue(ch.site_id),
          ch.cl,
          ch.seq,
        ]
      );
    }
    await normalizeMergedCustomerReferences(db);
    await db.execute("COMMIT");
  } catch (error) {
    try { await db.execute("ROLLBACK"); } catch { }
    throw error;
  }
}

export async function cacheBranchInventoryRows(items: BranchInventoryWithDetails[]): Promise<void> {
  if (items.length === 0) return;
  const db = await getDb();
  await ensureBranchInventorySchema(db);
  for (const item of items) {
    const now = item.updated_at ?? new Date().toISOString();
    const raw = item as unknown as Record<string, unknown>;
    const drugType = typeof raw.drug_type === "string" ? raw.drug_type : "otc";
    const requiresPrescription =
      raw.requires_prescription === true ||
      raw.requires_prescription === 1 ||
      raw.requires_prescription === "1" ||
      raw.requires_prescription === "true";
    await db.execute(
      `INSERT INTO drugs
        (id, organization_id, name, generic_name, brand_name, sku, barcode,
         category_id, drug_type, dosage_form, strength, manufacturer, supplier,
         requires_prescription, controlled_substance_schedule, ndc_code,
         unit_price, cost_price, markup_percentage, tax_rate, reorder_level,
         reorder_quantity, max_stock_level, unit_of_measure, description,
         usage_instructions, side_effects, contraindications, storage_conditions,
         is_active, is_deleted, sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, '', $2, NULL, NULL, $3, NULL,
         NULL, $4, NULL, NULL, NULL, NULL,
         $5, NULL, NULL,
         $6, NULL, NULL, 0, $7,
         50, NULL, 'unit', NULL,
         NULL, NULL, NULL, NULL,
         1, 0, 'synced', 1, NULL, $8, $8)
       ON CONFLICT(id) DO UPDATE SET
         name = excluded.name,
         sku = excluded.sku,
         drug_type = excluded.drug_type,
         requires_prescription = excluded.requires_prescription,
         unit_price = excluded.unit_price,
         reorder_level = excluded.reorder_level,
         updated_at = excluded.updated_at`,
      [
        item.drug_id,
        item.drug_name || item.drug_id,
        item.drug_sku ?? null,
        drugType,
        requiresPrescription ? 1 : 0,
        item.effective_unit_price ?? item.drug_unit_price ?? item.selling_price ?? 0,
        item.drug_reorder_level ?? 0,
        now,
      ]
    );

    await db.execute(
      `INSERT OR REPLACE INTO branch_inventory
        (id, branch_id, drug_id, quantity, reserved_quantity, location, selling_price,
         sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
      [
        item.id,
        item.branch_id,
        item.drug_id,
        item.quantity,
        item.reserved_quantity,
        item.location,
        item.selling_price,
        item.sync_status ?? "synced",
        item.sync_version ?? 1,
        item.synced_at ?? null,
        item.updated_at,
        item.created_at,
      ]
    );
  }
}

export async function cacheDrugs(items: Drug[]): Promise<void> {
  if (items.length === 0) return;
  const db = await getDb();
  for (const item of items) {
    await db.execute(
      `INSERT OR REPLACE INTO drugs
        (id, organization_id, name, generic_name, brand_name, sku, barcode,
         category_id, drug_type, dosage_form, strength, manufacturer, supplier,
         requires_prescription, controlled_substance_schedule, ndc_code,
         unit_price, cost_price, markup_percentage, tax_rate, reorder_level,
         reorder_quantity, max_stock_level, unit_of_measure, description,
         usage_instructions, side_effects, contraindications, storage_conditions,
         is_active, is_deleted, sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
               $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
               $29, $30, $31, $32, $33, $34, $35, $36)`,
      [
        item.id,
        item.organization_id,
        item.name,
        item.generic_name ?? null,
        item.brand_name ?? null,
        item.sku ?? null,
        item.barcode ?? null,
        item.category_id ?? null,
        item.drug_type ?? "otc",
        item.dosage_form ?? null,
        item.strength ?? null,
        item.manufacturer ?? null,
        item.supplier ?? null,
        item.requires_prescription ? 1 : 0,
        item.controlled_substance_schedule ?? null,
        item.ndc_code ?? null,
        item.unit_price,
        item.cost_price ?? null,
        item.markup_percentage ?? null,
        item.tax_rate ?? 0,
        item.reorder_level ?? 10,
        item.reorder_quantity ?? 50,
        item.max_stock_level ?? null,
        item.unit_of_measure ?? "unit",
        item.description ?? null,
        item.usage_instructions ?? null,
        item.side_effects ?? null,
        item.contraindications ?? null,
        item.storage_conditions ?? null,
        item.is_active ? 1 : 0,
        0,
        "synced",
        item.sync_version ?? 1,
        item.synced_at ?? null,
        item.updated_at,
        item.created_at,
      ]
    );
  }
}

export async function cacheBranchScopedDrugs(branchId: string, items: Drug[]): Promise<void> {
  await cacheDrugs(items);
  if (items.length === 0) return;

  const db = await getDb();
  await ensureBranchInventorySchema(db);
  const now = new Date().toISOString();
  for (const item of items) {
    const raw = item as unknown as Record<string, unknown>;
    const hasQuantityInfo =
      raw.quantity !== undefined ||
      raw.total_quantity !== undefined ||
      raw.available_quantity !== undefined;
    if (!hasQuantityInfo) continue;

    const quantity = Number(raw.quantity ?? raw.total_quantity ?? raw.available_quantity ?? 0) || 0;
    const reservedQuantity = Number(raw.reserved_quantity ?? 0) || 0;
    const sellingPrice = Number(raw.effective_unit_price ?? raw.unit_price ?? item.unit_price);

    await db.execute(
      `INSERT INTO branch_inventory
        (id, branch_id, drug_id, quantity, reserved_quantity, location, selling_price,
         sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, $2, $3, $4, $5, NULL, $6, 'synced', 1, NULL, $7, $7)
       ON CONFLICT(id) DO UPDATE SET
         quantity = CASE
           WHEN excluded.quantity > 0 THEN excluded.quantity
           ELSE branch_inventory.quantity
         END,
         reserved_quantity = CASE
           WHEN excluded.quantity > 0 THEN excluded.reserved_quantity
           ELSE branch_inventory.reserved_quantity
         END,
         selling_price = excluded.selling_price,
         updated_at = excluded.updated_at`,
      [
        `${branchId}:${item.id}`,
        branchId,
        item.id,
        quantity,
        reservedQuantity,
        Number.isFinite(sellingPrice) ? sellingPrice : item.unit_price,
        item.updated_at ?? now,
      ]
    );
  }
}

export async function cacheSales(items: Sale[]): Promise<void> {
  if (items.length === 0) return;
  const db = await getDb();
  for (const item of items) {
    await db.execute(
      `INSERT OR REPLACE INTO sales
        (id, organization_id, branch_id, sale_number, customer_id, customer_name,
         subtotal, discount_amount, tax_amount, total_amount, price_contract_id,
         contract_name, contract_discount_percentage, payment_method, payment_status,
         amount_paid, change_amount, payment_reference, prescription_id,
         prescription_number, prescriber_name, cashier_id, pharmacist_id,
         insurance_claim_number, patient_copay_amount, insurance_covered_amount,
         insurance_verified, insurance_verified_at, insurance_verified_by,
         notes, status, receipt_printed, receipt_emailed, items_json, items_count,
         sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
               $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27,
               $28, $29, $30, $31, $32, $33, $34, $35, $36, $37, $38, $39, $40)`,
      [
        item.id,
        item.organization_id,
        item.branch_id,
        item.sale_number,
        item.customer_id ?? null,
        item.customer_name ?? null,
        item.subtotal,
        item.discount_amount,
        item.tax_amount,
        item.total_amount,
        item.price_contract_id ?? null,
        item.contract_name ?? null,
        item.contract_discount_percentage ?? null,
        item.payment_method,
        item.payment_status,
        item.amount_paid ?? null,
        item.change_amount ?? null,
        item.payment_reference ?? null,
        item.prescription_id ?? null,
        item.prescription_number ?? null,
        item.prescriber_name ?? null,
        item.cashier_id,
        item.pharmacist_id ?? null,
        item.insurance_claim_number ?? null,
        item.patient_copay_amount ?? null,
        item.insurance_covered_amount ?? null,
        item.insurance_verified ? 1 : 0,
        item.insurance_verified_at ?? null,
        item.insurance_verified_by ?? null,
        item.notes ?? null,
        item.status,
        item.receipt_printed ? 1 : 0,
        item.receipt_emailed ? 1 : 0,
        JSON.stringify(item.items ?? []),
        item.items_count ?? (item.items ? item.items.reduce((sum, item) => sum + (item.quantity ?? 0), 0) : 0),
        "synced",
        item.sync_version ?? 1,
        item.synced_at ?? null,
        item.updated_at,
        item.created_at,
      ]
    );
  }
}
