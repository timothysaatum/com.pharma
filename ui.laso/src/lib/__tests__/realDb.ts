/**
 * realDb.ts — test helper.
 *
 * Wires the REAL localDb module to an in-memory SQLite through the Tauri
 * `invoke` boundary, so every helper (getEventPullSeq, setEventPullSeq,
 * isLocallyAuthored, the failure table, the projectors) runs its production code
 * path against a real database.
 *
 * Do not reach for `vi.mock("@/lib/localDb", importActual)` instead: that
 * returns the genuine module, whose helpers call the genuine internal getDb(),
 * which resolves to MockDb (a no-op) outside Tauri. The test's adapter is then
 * never used and writes vanish silently, which looks exactly like a production
 * bug where none exists. Mocking `invoke` is the seam localDb is actually built
 * around.
 *
 * Why the real module rather than hand-written DDL: the whole point of the
 * sellable_quantity work is that the column exists, is NOT NULL DEFAULT 0, and
 * sits beside drug_batches and stock_leases. A fixture that invents its own
 * schema passes while the device stays broken, which is how the test this file
 * replaces managed to be green.
 *
 * node:sqlite ships with Node >=22, so this adds no dependency.
 */
import { DatabaseSync } from "node:sqlite";
import { vi } from "vitest";

export const TEST_BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
export const OTHER_BRANCH = "11111111-2222-3333-4444-555555555555";
export const GEBEDOL = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";
export const THIS_TERMINAL = "TERM-TEST-THIS";
export const OTHER_TERMINAL = "TERM-TEST-OTHER";

/** Handle shared with the invoke mock. */
let handle: DatabaseSync | null = null;
let migrated = false;

/** Adapt one statement's params: localDb speaks $1..$n, node:sqlite wants ?. */
function bind(sql: string, values: unknown[]): { sql: string; values: unknown[] } {
  if (!/\$\d+/.test(sql)) return { sql, values: [...values] };
  const out: unknown[] = [];
  return {
    sql: sql.replace(/\$(\d+)/g, (_, i: string) => {
      out.push(values[parseInt(i, 10) - 1]);
      return "?";
    }),
    values: out,
  };
}

/**
 * Install the invoke bridge. Call once per test file, before importing
 * localDb. `seed` runs after the migration chain, so it can insert rows into
 * the real schema.
 */
export async function installRealDb(seed?: (raw: DatabaseSync) => void): Promise<DatabaseSync> {
  handle = new DatabaseSync(":memory:");

  vi.mock("@tauri-apps/api/core", () => ({
    invoke: async (command: string, args: Record<string, unknown>) => {
      if (!handle) throw new Error("realDb: invoke used before installRealDb()");
      const sql = String(args.sql ?? "");
      const values = (args.values as unknown[]) ?? [];

      switch (command) {
        case "db_execute": {
          const n = bind(sql, values);
          const r = handle.prepare(n.sql).run(...(n.values as never[]));
          return { rowsAffected: Number(r.changes), lastInsertId: Number(r.lastInsertRowid) };
        }
        case "db_select": {
          const n = bind(sql, values);
          return handle.prepare(n.sql).all(...(n.values as never[]));
        }
        case "db_execute_batch":
          handle.exec(sql);
          return null;
        case "db_execute_transaction": {
          const statements = (args.statements as Array<{ sql: string; values?: unknown[] }>) ?? [];
          const results: Array<{ rowsAffected: number; lastInsertId: number }> = [];
          handle.exec("BEGIN");
          try {
            for (const st of statements) {
              const n = bind(st.sql, st.values ?? []);
              const r = handle.prepare(n.sql).run(...(n.values as never[]));
              results.push({ rowsAffected: Number(r.changes), lastInsertId: Number(r.lastInsertRowid) });
            }
            handle.exec("COMMIT");
          } catch (e) {
            handle.exec("ROLLBACK");
            throw e;
          }
          return results;
        }
        default:
          throw new Error(`realDb: unhandled Tauri command ${command}`);
      }
    },
  }));

  // localDb computes IS_TAURI at module load; without this it returns MockDb.
  vi.stubGlobal("window", {
    __TAURI_INTERNALS__: {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => true,
  });

  const { getDb, runMigrations } = await import("@/lib/localDb");
  const db = await getDb();
  await runMigrations(db);
  migrated = true;
  seed?.(handle);
  return handle;
}

/** The live handle, for direct assertions. */
export function rawDb(): DatabaseSync {
  if (!handle) throw new Error("realDb: installRealDb() has not run");
  return handle;
}

/** Truncate the tables these tests assert on, keeping the schema. */
export function resetTables(tables: string[]): void {
  const h = rawDb();
  h.exec("PRAGMA foreign_keys = OFF");
  for (const t of tables) h.exec(`DELETE FROM ${t}`);
}

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

export interface InventoryOpts {
  id?: string;
  branchId?: string;
  drugId?: string;
  quantity?: number;
  reserved?: number;
  sellable?: number;
}

export function insertInventory(raw: DatabaseSync, opts: InventoryOpts = {}): void {
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
  opts: { id?: string; branchId?: string; drugId?: string; remaining?: number; expiry?: string } = {}
): void {
  raw
    .prepare(
      `INSERT INTO drug_batches
         (id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
          manufacturing_date, expiry_date, sync_status, sync_version,
          synced_at, updated_at, created_at)
       VALUES (?,?,?,'B1',?,?,'2020-01-01',?,'synced',1,NULL,?,?)`
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

/** The sellable_quantity persisted on a row. */
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

export { migrated };