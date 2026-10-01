/**
 * realDb.ts — test helper.
 *
 * Builds a REAL in-memory SQLite database using the production migration chain
 * (runMigrations) and adapts node:sqlite to the localDb `Database` interface.
 *
 * Why the real migration chain: hand-copied DDL in a test drifts silently from
 * localDb.ts, and the whole point of the sellable_quantity work is that the
 * column exists, is NOT NULL DEFAULT 0, and sits next to drug_batches and
 * stock_leases. A fixture that invents its own schema would pass while the
 * device stayed broken — which is exactly how the test this file replaces
 * managed to be green.
 *
 * node:sqlite is used because it ships with Node (>=22); adding better-sqlite3
 * would mean a new dependency for tests only.
 */
import { DatabaseSync } from "node:sqlite";
import type { Database } from "@/lib/localDb";

/** Adapt node:sqlite to the localDb Database interface ($n -> ?). */
export function adaptSqlite(raw: DatabaseSync): Database {
  const normalize = (sql: string, values: unknown[]) => {
    const out: unknown[] = [];
    if (/\$\d+/.test(sql)) {
      return {
        sql: sql.replace(/\$(\d+)/g, (_, i: string) => {
          out.push(values[parseInt(i, 10) - 1]);
          return "?";
        }),
        values: out,
      };
    }
    return { sql, values: [...values] };
  };

  const adapter: Database = {
    execute: async (sql: string, values: unknown[] = []) => {
      const n = normalize(sql, values);
      const r = raw.prepare(n.sql).run(...(n.values as never[]));
      return { rowsAffected: Number(r.changes), lastInsertId: Number(r.lastInsertRowid) };
    },
    select: async <T>(sql: string, values: unknown[] = []): Promise<T> => {
      const n = normalize(sql, values);
      return raw.prepare(n.sql).all(...(n.values as never[])) as T;
    },
    execute_batch: async (sql: string) => {
      raw.exec(sql);
    },
    load: async () => adapter,
  };
  return adapter;
}

export const TEST_BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
export const OTHER_BRANCH = "11111111-2222-3333-4444-555555555555";
export const GEBEDOL = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";
export const THIS_TERMINAL = "TERM-TEST-THIS";
export const OTHER_TERMINAL = "TERM-TEST-OTHER";

/** ISO date `days` from now; negative is in the past. */
export function daysFromNow(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** ISO timestamp `hours` from now. */
export function hoursFromNow(hours: number): string {
  return new Date(Date.now() + hours * 3_600_000).toISOString();
}

export interface Fixture {
  raw: DatabaseSync;
  db: Database;
}

/**
 * Create a migrated in-memory database with the given rows pre-inserted.
 * `seed` receives a direct synchronous handle for terse setup.
 */
export async function makeFixture(
  seed?: (raw: DatabaseSync) => void,
  options: { skipMigrations?: boolean } = {}
): Promise<Fixture> {
  const raw = new DatabaseSync(":memory:");
  const db = adaptSqlite(raw);
  if (!options.skipMigrations) {
    const { runMigrations } = await import("@/lib/localDb");
    await runMigrations(db);
  }
  seed?.(raw);
  return { raw, db };
}

/** Insert a branch_inventory row. sellable_quantity defaults to whatever the
 *  caller passes so a test can assert a stale 0 is corrected. */
export function insertInventory(
  raw: DatabaseSync,
  opts: {
    id?: string;
    branchId?: string;
    drugId?: string;
    quantity?: number;
    reserved?: number;
    sellable?: number;
  } = {}
): void {
  raw
    .prepare(
      `INSERT INTO branch_inventory
         (id, branch_id, drug_id, quantity, reserved_quantity, sellable_quantity,
          location, selling_price, sync_status, sync_version, synced_at,
          updated_at, created_at)
       VALUES (?,?,?,?,?,?,NULL,NULL,'synced',1,NULL,?,?)`
    )
    .run(
      opts.id ?? "inv-1",
      opts.branchId ?? TEST_BRANCH,
      opts.drugId ?? GEBEDOL,
      opts.quantity ?? 0,
      opts.reserved ?? 0,
      opts.sellable ?? 0,
      "2026-01-01T00:00:00Z",
      "2026-01-01T00:00:00Z"
    );
}

export function insertBatch(
  raw: DatabaseSync,
  opts: {
    id?: string;
    branchId?: string;
    drugId?: string;
    remaining?: number;
    expiry?: string;
  } = {}
): void {
  raw
    .prepare(
      `INSERT INTO drug_batches
         (id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
          manufacturing_date, expiry_date, cost_price, selling_price, supplier,
          purchase_order_id, sync_status, sync_version, synced_at, updated_at, created_at)
       VALUES (?,?,?,'B1',?,?,'2020-01-01',?,NULL,NULL,NULL,NULL,'synced',1,NULL,?,?)`
    )
    .run(
      opts.id ?? "batch-1",
      opts.branchId ?? TEST_BRANCH,
      opts.drugId ?? GEBEDOL,
      opts.remaining ?? 0,
      opts.remaining ?? 0,
      opts.expiry ?? daysFromNow(365),
      "2026-01-01T00:00:00Z",
      "2026-01-01T00:00:00Z"
    );
}

export function insertLease(
  raw: DatabaseSync,
  opts: {
    id?: string;
    branchId?: string;
    drugId?: string;
    terminalId?: string;
    leased?: number;
    consumed?: number;
    expiresAt?: string;
    status?: string;
  } = {}
): void {
  raw
    .prepare(
      `INSERT INTO stock_leases
         (id, branch_id, drug_id, terminal_id, leased_quantity, consumed_quantity,
          expires_at, status, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?)`
    )
    .run(
      opts.id ?? "lease-1",
      opts.branchId ?? TEST_BRANCH,
      opts.drugId ?? GEBEDOL,
      opts.terminalId ?? OTHER_TERMINAL,
      opts.leased ?? 0,
      opts.consumed ?? 0,
      opts.expiresAt ?? hoursFromNow(24),
      opts.status ?? "active",
      "2026-01-01T00:00:00Z",
      "2026-01-01T00:00:00Z"
    );
}

/** Read back the sellable_quantity persisted on a row. */
export function readSellable(
  raw: DatabaseSync,
  branchId = TEST_BRANCH,
  drugId = GEBEDOL
): number | undefined {
  const row = raw
    .prepare("SELECT sellable_quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?")
    .get(branchId, drugId) as { sellable_quantity: number } | undefined;
  return row?.sellable_quantity;
}