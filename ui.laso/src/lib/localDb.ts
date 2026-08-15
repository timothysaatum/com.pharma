/**
 * localDb.ts
 * ==========
 * Local SQLite database via tauri-plugin-sql.
 * Mirrors the server's schema for the tables the branch owns or caches.
 */

import type { Sale } from "@/types";
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

/**
 * Raised when a Tauri `invoke` into the local SQLite layer does not settle in
 * time. Callers can distinguish this from a genuine SQL error and surface a
 * retryable state instead of blocking forever.
 */
export class LocalDbTimeoutError extends Error {
  constructor(public readonly command: string, public readonly timeoutMs: number) {
    super(`Local database call "${command}" timed out after ${timeoutMs}ms`);
    this.name = "LocalDbTimeoutError";
  }
}

/** Hard ceiling for any single local-DB round trip. */
const LOCAL_DB_TIMEOUT_MS = 15_000;

/**
 * `invoke` with a hard timeout.
 *
 * A Tauri IPC promise can hang indefinitely when its callback id is dropped —
 * e.g. the webview reloaded while Rust was mid-operation. Without a ceiling
 * every caller inherits that hang: `withTimeout`'s cache fallback is not
 * itself bounded, so a stalled read leaves loaders spinning with no error.
 * Rejecting here converts a silent hang into a surfaced, retryable failure.
 */
async function invokeWithTimeout<T>(command: string, args: Record<string, unknown>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      invoke<T>(command, args),
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new LocalDbTimeoutError(command, LOCAL_DB_TIMEOUT_MS)),
          LOCAL_DB_TIMEOUT_MS,
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
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
      const result = await invokeWithTimeout<ExecResult>("db_execute", { sql, values: values ?? [] });
      return result;
    },
    select: async <T>(sql: string, values?: unknown[]): Promise<T> => {
      const result = await invokeWithTimeout<T>("db_select", { sql, values: values ?? [] });
      return result;
    },
    execute_batch: async (sql: string): Promise<void> => {
      await invokeWithTimeout<void>("db_execute_batch", { sql });
    },
    executeTransaction: async (
      statements: DbTransactionStatement[],
    ): Promise<ExecResult[]> => invokeWithTimeout<ExecResult[]>("db_execute_transaction", {
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
const MAX_KNOWN_SCHEMA_VERSION = 28;

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
      if (user_version < 23) await migrate_v23(db);
      if (user_version < 24) await migrate_v24(db);
      if (user_version < 25) await migrate_v25(db);
      if (user_version < 26) await migrate_v26(db);
      if (user_version < 27) await migrate_v27(db);
      if (user_version < 28) await migrate_v28(db);
      if (user_version < 29) await migrate_v29(db);
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
      record_id   TEXT NOT NULL,
      reason      TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (table_name, db_version, record_id)
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
    const CRR_TABLES = [
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
            sellable_quantity INTEGER NOT NULL DEFAULT 0,
            location          TEXT,
            selling_price     REAL,
            sync_status       TEXT NOT NULL DEFAULT 'synced',
            sync_version      INTEGER NOT NULL DEFAULT 1,
            synced_at         TEXT,
            updated_at        TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL DEFAULT ''
          )`,
          cols: `id, branch_id, drug_id, quantity, reserved_quantity, sellable_quantity, location,
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
            address                 TEXT,
            allergies               TEXT,
            chronic_conditions      TEXT,
            preferred_contact_method TEXT,
            marketing_consent       INTEGER NOT NULL DEFAULT 0,
            insurance_card_image_url TEXT,
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
            date_of_birth, address, allergies, chronic_conditions,
            preferred_contact_method, marketing_consent, insurance_card_image_url,
            loyalty_points, loyalty_tier, insurance_provider_id,
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

    await db.execute("PRAGMA user_version = 22");
    await db.execute("COMMIT");
    console.log("[localDb] Migration v22 complete — CRR tables rebuilt with NOT NULL PKs");
  } catch (error) {
    try { await db.execute("ROLLBACK"); } catch { }
    console.error("[localDb] Migration v22 failed:", error);
    throw error;
  }
}

/// Migration v23: generalize suppressed_crr_changes beyond just sales.
/// `sale_id` was the only producer when this table was created
/// (offlineSalesManager.ts, suppressing local sale-projection writes from
/// re-reaching the server). It's now also used to suppress permanently-
/// rejected prescriptions/purchase_orders (see suppressPermanentlyRejectedCrrRow)
/// -- neither is a sale, so the old name was actively misleading. The
/// filter query (getCrrPushChangesFromDb) only ever matched on
/// (table_name, db_version), never on this column's value, so the rename
/// changes nothing about existing suppression behavior.
export async function migrate_v23(db: Database): Promise<void> {
  await ensureSuppressedCrrChangesSchema(db);
  try {
    await db.execute(
      "ALTER TABLE suppressed_crr_changes RENAME COLUMN sale_id TO record_id",
    );
  } catch {
    // Already renamed by a prior interrupted attempt at this migration.
  }
  await db.execute("PRAGMA user_version = 23");
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

// ── Compatibility stubs for legacy components ────────────────────────────────

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

export interface QueuedConflict {
  table_name: string;
  record_id: string;
  conflict: any;
  local_data: Record<string, unknown>;
}

export interface QueuedFailure {
  table_name: string;
  record_id: string;
  attempts: number;
  error: string | null;
  is_blocked: boolean;
  local_data: Record<string, unknown>;
}

export function isQueuedRecordInScope(_row: any, _scope?: any): boolean {
  return true;
}

export function notifySyncQueueChanged(): void {}

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

let _device_id: string | null = null;

/** Get the stable local device ID (cached after first call). */
export async function getDeviceId(): Promise<string> {
  if (_device_id) return _device_id;
  const db = await getDb();
  const rows = await db.select<{ value: string }[]>(
    "SELECT value FROM sync_meta WHERE key = 'device_id'"
  );
  if (rows && rows.length > 0 && rows[0].value) {
    _device_id = rows[0].value;
  } else {
    _device_id = crypto.randomUUID();
    await db.execute(
      "INSERT INTO sync_meta (key, value) VALUES ('device_id', $1)",
      [_device_id]
    );
  }
  return _device_id;
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

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION V24 — event_outbox (Phase 2 event-sourced sync spine)
// ─────────────────────────────────────────────────────────────────────────────
//
// Stores locally-authored events waiting to be pushed to the server. The
// hash chain (hash_prev / hash_self) is computed in eventEnvelope.ts before
// appendToOutbox() is called. `status` tracks push progress:
//   pending            → not yet pushed
//   accepted           → server confirmed, projector ran
//   accepted_deferred  → server accepted but deps still unresolved
//   rejected_permanent → server will never accept; requires operator action
//   failed             → transient error; will be retried next cycle
//
// event_pull_seq in sync_meta tracks the highest event_log.seq successfully
// pulled from the server and applied locally.

async function migrate_v24(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS event_outbox (
      event_id          TEXT NOT NULL PRIMARY KEY,
      aggregate_type    TEXT NOT NULL,
      event_type        TEXT NOT NULL,
      aggregate_id      TEXT NOT NULL,
      org_id            TEXT NOT NULL,
      branch_id         TEXT NOT NULL,
      authored_by       TEXT NOT NULL,
      authored_at       TEXT NOT NULL,
      schema_version    INTEGER NOT NULL DEFAULT 1,
      payload           TEXT NOT NULL,
      dependencies      TEXT NOT NULL DEFAULT '[]',
      hash_prev         TEXT NOT NULL,
      hash_self         TEXT NOT NULL,
      status            TEXT NOT NULL DEFAULT 'pending',
      attempts          INTEGER NOT NULL DEFAULT 0,
      error_code        TEXT,
      error_message     TEXT,
      created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
  `);
  await db.execute(
    "CREATE INDEX IF NOT EXISTS ix_event_outbox_status ON event_outbox (status, created_at)"
  );
  await db.execute("PRAGMA user_version = 24");
}

// ─────────────────────────────────────────────────────────────────────────────
// EVENT OUTBOX HELPERS
// ─────────────────────────────────────────────────────────────────────────────

export interface OutboxEvent {
  event_id: string;
  aggregate_type: string;
  event_type: string;
  aggregate_id: string;
  org_id: string;
  branch_id: string;
  authored_by: string;
  authored_at: string;
  schema_version: number;
  payload: Record<string, unknown>;
  dependencies: string[];
  hash_prev: string;
  hash_self: string;
  status: string;
  attempts: number;
  error_code: string | null;
  error_message: string | null;
}

/** Return the hash_self of the last event written to the outbox, or GENESIS_HASH. */
export async function getOutboxTailHash(): Promise<string> {
  const db = await getDb();
  const rows = await db.select<{ hash_self: string }[]>(
    "SELECT hash_self FROM event_outbox ORDER BY created_at DESC, event_id DESC LIMIT 1"
  );
  return rows?.[0]?.hash_self ?? "0".repeat(64);
}

/** Append a fully-formed event envelope to the outbox. */
export async function appendToOutbox(event: OutboxEvent): Promise<void> {
  const db = await getDb();
  await db.execute(
    `INSERT INTO event_outbox
      (event_id, aggregate_type, event_type, aggregate_id,
       org_id, branch_id, authored_by, authored_at, schema_version,
       payload, dependencies, hash_prev, hash_self, status)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'pending')
     ON CONFLICT(event_id) DO NOTHING`,
    [
      event.event_id,
      event.aggregate_type,
      event.event_type,
      event.aggregate_id,
      event.org_id,
      event.branch_id,
      event.authored_by,
      event.authored_at,
      event.schema_version,
      JSON.stringify(event.payload),
      JSON.stringify(event.dependencies),
      event.hash_prev,
      event.hash_self,
    ]
  );
}

/** Return up to `limit` events that need to be pushed (ordered oldest-first). */
export async function getPendingOutboxEvents(limit = 500): Promise<OutboxEvent[]> {
  const db = await getDb();
  const rows = await db.select<Array<Record<string, unknown>>>(
    `SELECT event_id, aggregate_type, event_type, aggregate_id,
            org_id, branch_id, authored_by, authored_at, schema_version,
            payload, dependencies, hash_prev, hash_self,
            status, attempts, error_code, error_message
       FROM event_outbox
      WHERE status IN ('pending', 'failed', 'accepted_deferred')
      ORDER BY created_at ASC, event_id ASC
      LIMIT $1`,
    [limit]
  );
  return rows.map((r) => ({
    event_id: r.event_id as string,
    aggregate_type: r.aggregate_type as string,
    event_type: r.event_type as string,
    aggregate_id: r.aggregate_id as string,
    org_id: r.org_id as string,
    branch_id: r.branch_id as string,
    authored_by: r.authored_by as string,
    authored_at: r.authored_at as string,
    schema_version: r.schema_version as number,
    payload: JSON.parse(r.payload as string),
    dependencies: JSON.parse(r.dependencies as string),
    hash_prev: r.hash_prev as string,
    hash_self: r.hash_self as string,
    status: r.status as string,
    attempts: r.attempts as number,
    error_code: (r.error_code ?? null) as string | null,
    error_message: (r.error_message ?? null) as string | null,
  }));
}

/** Update the push result for an outbox event. */
export async function markOutboxResult(
  eventId: string,
  status: string,
  error?: { code: string; message: string },
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `UPDATE event_outbox
        SET status = $1,
            attempts = attempts + 1,
            error_code = $2,
            error_message = $3
      WHERE event_id = $4`,
    [status, error?.code ?? null, error?.message ?? null, eventId]
  );
}

/** Return the number of outbox events pending push. */
export async function getPendingOutboxCount(): Promise<number> {
  const db = await getDb();
  const rows = await db.select<{ count: number }[]>(
    "SELECT COUNT(*) as count FROM event_outbox WHERE status IN ('pending', 'failed', 'accepted_deferred')"
  );
  return rows?.[0]?.count ?? 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// EVENT PULL CURSOR
// ─────────────────────────────────────────────────────────────────────────────

const EVENT_PULL_SEQ_KEY = "event_pull_seq";

/** Last event_log.seq successfully pulled and applied from the server. */
export async function getEventPullSeq(): Promise<number> {
  const db = await getDb();
  const rows = await db.select<{ value: string }[]>(
    "SELECT value FROM sync_meta WHERE key = $1",
    [EVENT_PULL_SEQ_KEY]
  );
  return rows?.[0]?.value ? Number(rows[0].value) : 0;
}

export async function setEventPullSeq(seq: number): Promise<void> {
  const db = await getDb();
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
    [EVENT_PULL_SEQ_KEY, String(seq)]
  );
}

/**
 * Returns true when the given event_id is present in the local outbox,
 * meaning this client authored the event and already applied it locally.
 * Used by pullEvents() to skip re-applying own events.
 */
export async function isLocallyAuthored(eventId: string): Promise<boolean> {
  const db = await getDb();
  const rows = await db.select<{ event_id: string }[]>(
    "SELECT event_id FROM event_outbox WHERE event_id = $1 LIMIT 1",
    [eventId]
  );
  return rows.length > 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION v25 — version_vector + pending_conflicts
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v25(db: Database): Promise<void> {
  // version_vector columns for the three reference-data tables.
  // ALTER TABLE ... ADD COLUMN IF NOT EXISTS is available in SQLite 3.37+.
  for (const table of ["customers", "drugs", "drug_categories"]) {
    try {
      await db.execute(
        `ALTER TABLE ${table} ADD COLUMN version_vector TEXT NOT NULL DEFAULT '{}'`
      );
    } catch {
      // Column already exists — safe to ignore.
    }
  }

  // Local mirror of server-side unresolved_conflicts. Populated during
  // pullEvents() so pharmacists can see conflicts without an extra API call.
  await db.execute(`
    CREATE TABLE IF NOT EXISTS pending_conflicts (
      id              TEXT PRIMARY KEY,
      org_id          TEXT NOT NULL,
      aggregate_type  TEXT NOT NULL,
      aggregate_id    TEXT NOT NULL,
      event_id        TEXT,
      local_vector    TEXT NOT NULL DEFAULT '{}',
      local_snapshot  TEXT NOT NULL DEFAULT '{}',
      incoming_vector TEXT NOT NULL DEFAULT '{}',
      incoming_payload TEXT NOT NULL DEFAULT '{}',
      status          TEXT NOT NULL DEFAULT 'pending',
      resolved_at     TEXT,
      created_at      TEXT NOT NULL
    )
  `);
  await db.execute(
    "CREATE INDEX IF NOT EXISTS ix_pending_conflicts_status ON pending_conflicts (status, created_at)"
  );

  await db.execute("PRAGMA user_version = 25");
}

async function migrate_v26(db: Database): Promise<void> {
  // Drop cr-sqlite shadow tables now that CRR has been retired.
  // These tables only exist on devices where the crsqlite extension was
  // previously loaded; DROP IF EXISTS makes this safe on fresh installs.
  for (const t of [
    "crsql_changes",
    "crsql_clock",
    "crsql_pack_columns",
    "suppressed_crr_changes",
    "crr_audit_uploads",
    "customer_merge_directives",
  ]) {
    try {
      await db.execute(`DROP TABLE IF EXISTS ${t}`);
    } catch {
      // Ignore — table may not exist or extension may be absent.
    }
  }
  await db.execute("PRAGMA user_version = 26");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION v27 — add customer columns required by localProjectors._customerCreated
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v27(db: Database): Promise<void> {
  // These columns were missing from the customers table schema (never added by
  // any earlier migration) but are referenced by _customerCreated in
  // localProjectors.ts. Add them idempotently — ALTER TABLE fails if the column
  // already exists; we catch and ignore that error.
  const customerCols: Array<[string, string]> = [
    ["address", "TEXT"],
    ["allergies", "TEXT"],
    ["chronic_conditions", "TEXT"],
    ["preferred_contact_method", "TEXT"],
    ["marketing_consent", "INTEGER NOT NULL DEFAULT 0"],
    ["insurance_card_image_url", "TEXT"],
  ];
  for (const [col, def] of customerCols) {
    try {
      await db.execute(`ALTER TABLE customers ADD COLUMN ${col} ${def}`);
    } catch {
      // Column already exists — safe to ignore.
    }
  }
  await db.execute("PRAGMA user_version = 27");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION v28 — add stock_leases table for Phase 4 stock partitioning
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v28(db: Database): Promise<void> {
  await db.execute(`
    CREATE TABLE IF NOT EXISTS stock_leases (
      id                 TEXT PRIMARY KEY,
      branch_id          TEXT NOT NULL,
      drug_id            TEXT NOT NULL,
      terminal_id        TEXT NOT NULL,
      leased_quantity    INTEGER NOT NULL DEFAULT 0,
      consumed_quantity  INTEGER NOT NULL DEFAULT 0,
      expires_at         TEXT NOT NULL,
      status             TEXT NOT NULL DEFAULT 'active',
      created_at         TEXT NOT NULL,
      updated_at         TEXT NOT NULL
    );
  `);
  await db.execute("PRAGMA user_version = 28");
}

// ─────────────────────────────────────────────────────────────────────────────
// MIGRATION v29 — drop legacy sync_queue
// ─────────────────────────────────────────────────────────────────────────────

async function migrate_v29(db: Database): Promise<void> {
  try {
    await db.execute("DROP TABLE IF EXISTS sync_queue");
  } catch {}
  await db.execute("PRAGMA user_version = 29");
}

// ─────────────────────────────────────────────────────────────────────────────
// VERSION VECTOR HELPERS
// ─────────────────────────────────────────────────────────────────────────────

export type VectorClock = Record<string, number>;

/** Read the stored version_vector for a row. Returns {} if not found. */
export async function getVersionVector(
  table: "customers" | "drugs" | "drug_categories",
  id: string
): Promise<VectorClock> {
  const db = await getDb();
  const rows = await db.select<{ version_vector: string }[]>(
    `SELECT version_vector FROM ${table} WHERE id = $1 LIMIT 1`,
    [id]
  );
  if (!rows.length) return {};
  try {
    return JSON.parse(rows[0].version_vector) as VectorClock;
  } catch {
    return {};
  }
}

/** Persist a version_vector for a row after applying a pulled event. */
export async function setVersionVector(
  table: "customers" | "drugs" | "drug_categories",
  id: string,
  vec: VectorClock
): Promise<void> {
  const db = await getDb();
  await db.execute(
    `UPDATE ${table} SET version_vector = $1 WHERE id = $2`,
    [JSON.stringify(vec), id]
  );
}

/** Bump own branch component and return the new vector (does NOT persist). */
export function bumpVector(vec: VectorClock, branchId: string): VectorClock {
  return { ...vec, [branchId]: (vec[branchId] ?? 0) + 1 };
}

// ─────────────────────────────────────────────────────────────────────────────
// PENDING CONFLICTS HELPERS
// ─────────────────────────────────────────────────────────────────────────────

export interface PendingConflict {
  id: string;
  org_id: string;
  aggregate_type: string;
  aggregate_id: string;
  event_id: string | null;
  local_vector: VectorClock;
  local_snapshot: Record<string, unknown>;
  incoming_vector: VectorClock;
  incoming_payload: Record<string, unknown>;
  status: string;
  resolved_at: string | null;
  created_at: string;
}

/** Replace the local conflict cache with the latest data from the server. */
export async function upsertPendingConflicts(conflicts: PendingConflict[]): Promise<void> {
  const db = await getDb();
  for (const c of conflicts) {
    await db.execute(
      `INSERT INTO pending_conflicts
         (id, org_id, aggregate_type, aggregate_id, event_id,
          local_vector, local_snapshot, incoming_vector, incoming_payload,
          status, resolved_at, created_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       ON CONFLICT(id) DO UPDATE SET
         status = excluded.status,
         resolved_at = excluded.resolved_at`,
      [
        c.id, c.org_id, c.aggregate_type, c.aggregate_id, c.event_id ?? null,
        JSON.stringify(c.local_vector),
        JSON.stringify(c.local_snapshot),
        JSON.stringify(c.incoming_vector),
        JSON.stringify(c.incoming_payload),
        c.status, c.resolved_at ?? null, c.created_at,
      ]
    );
  }
}

/** Return all pending (unresolved) event-spine conflicts from the local cache. */
export async function getEventConflicts(): Promise<PendingConflict[]> {
  const db = await getDb();
  const rows = await db.select<Record<string, string>[]>(
    "SELECT * FROM pending_conflicts WHERE status = 'pending' ORDER BY created_at DESC"
  );
  return rows.map((r) => ({
    id: r.id,
    org_id: r.org_id,
    aggregate_type: r.aggregate_type,
    aggregate_id: r.aggregate_id,
    event_id: r.event_id ?? null,
    local_vector: _parseVec(r.local_vector),
    local_snapshot: _parseObj(r.local_snapshot),
    incoming_vector: _parseVec(r.incoming_vector),
    incoming_payload: _parseObj(r.incoming_payload),
    status: r.status,
    resolved_at: r.resolved_at ?? null,
    created_at: r.created_at,
  }));
}

function _parseVec(s: string): VectorClock {
  try { return JSON.parse(s) as VectorClock; } catch { return {}; }
}

function _parseObj(s: string): Record<string, unknown> {
  try { return JSON.parse(s) as Record<string, unknown>; } catch { return {}; }
}
