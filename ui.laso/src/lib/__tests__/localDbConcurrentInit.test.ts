/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Regression coverage for a real bug reported after pulling to a fresh
 * machine: "cannot start a transaction within a transaction", thrown from
 * runMigrations() and then repeating forever on every subsequent sync
 * attempt.
 *
 * Root cause: `_db` was assigned synchronously the moment the *first*
 * getDb() call started — before `load()` (runMigrations +
 * ensureCrrTablesEnabled) had done any real work, let alone finished. A
 * second, concurrent caller (SyncEngine.start() fires loadPersistedQueueState()
 * without awaiting it, then immediately calls sync(), which calls getDb()
 * again via pushCrr()/pullCrr()) took the `if (_db) return _db` fast path
 * and got back a connection it could start querying immediately — while the
 * first caller's migration was still mid-flight on the exact same
 * connection. Every individual db.execute() call is its own separate Tauri
 * IPC round trip, and the Rust-side connection Mutex is only held for one
 * statement at a time, not for a whole logical multi-step transaction — so
 * the two callers' statements interleaved on the shared connection, and
 * whichever side's BEGIN landed second failed with the raw SQLite error
 * reproduced here (independently confirmed against a real rusqlite
 * connection using the exact same per-statement locking pattern).
 *
 * The fix: getDb() now caches the in-flight initialization *promise*, not
 * just the eventual resolved value, so a concurrent caller cannot obtain a
 * usable connection — and therefore cannot issue a query — until the SAME
 * single initialization has actually finished. It also clears that cache on
 * failure so a later call can retry instead of repeating the same failure
 * forever.
 */

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

function installTauriGlobal() {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

function removeTauriGlobal() {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
}

/** Generic, safe-by-default invoke mock: schema already at the latest known
 * version (skips every numbered migration, exercising only the
 * unconditional ensure*Schema/CRR-enable calls), every existence check
 * reports "does not exist" (so those code paths take their no-op branches),
 * every write succeeds. Every call is appended to `log` in the exact order
 * invoke() receives it, with a `firstReadDelayMs` on the very first PRAGMA
 * user_version read — the first thing runMigrations() does — so a real
 * concurrency window is forced open deterministically instead of relying on
 * incidental timing.
 */
function installMigrationFriendlyInvokeMock(log: string[], options: { firstReadDelayMs?: number } = {}) {
  let firstReadSeen = false;
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const sql = String(args?.sql ?? "");
    if (cmd === "db_select" && sql.includes("PRAGMA user_version")) {
      log.push("read:user_version");
      if (!firstReadSeen && options.firstReadDelayMs) {
        firstReadSeen = true;
        await new Promise((resolve) => setTimeout(resolve, options.firstReadDelayMs));
      }
      return [{ user_version: 22 }];
    }
    if (cmd === "db_select") {
      log.push(`select:${sql.slice(0, 30)}`);
      return [];
    }
    if (cmd === "db_execute") {
      log.push(`execute:${sql.slice(0, 30)}`);
      return { rowsAffected: 0 };
    }
    if (cmd === "db_execute_batch") {
      log.push("execute_batch");
      return undefined;
    }
    throw new Error(`unexpected invoke call in test: ${cmd}`);
  });
}

describe("getDb() concurrent initialization", () => {
  beforeEach(() => {
    vi.resetModules();
    invokeMock.mockReset();
    installTauriGlobal();
  });

  afterEach(() => {
    removeTauriGlobal();
  });

  it("baseline: a solo getDb() completes a known-shape sequence of IPC calls", async () => {
    const log: string[] = [];
    installMigrationFriendlyInvokeMock(log);
    const { getDb } = await import("@/lib/localDb");

    await getDb();

    // Sanity: migrations/ensure*Schema really did run (more than just the
    // version read) — otherwise the later assertion would pass vacuously.
    expect(log.length).toBeGreaterThan(1);
    expect(log[0]).toBe("read:user_version");
  });

  it("a concurrent second caller cannot query the connection before the first caller's initialization has fully finished", async () => {
    const log: string[] = [];
    installMigrationFriendlyInvokeMock(log, { firstReadDelayMs: 30 });
    const { getDb } = await import("@/lib/localDb");

    // Exactly the real-world shape: SyncEngine.start() fires
    // loadPersistedQueueState() (-> getDb()) without awaiting it, then
    // immediately calls sync() (-> pullCrr() -> getDb() -> a real query).
    const first = getDb();
    const second = getDb().then((db) => db.select("SELECT 1 AS marker"));

    await Promise.all([first, second]);

    const markerIndex = log.indexOf("select:SELECT 1 AS marker");
    expect(markerIndex).toBeGreaterThan(-1);

    // The second caller's own query must not appear anywhere in the middle
    // of initialization — it can only be issued once the whole thing (every
    // migration/ensure*Schema IPC call) has already completed. In the buggy
    // version this index was 1 (right after the still-pending delayed
    // PRAGMA read), proving the second caller raced ahead of migrations.
    const initCallCountBeforeMarker = log.slice(0, markerIndex).length;
    const totalInitCalls = log.length - 1; // exclude the marker call itself
    expect(initCallCountBeforeMarker).toBe(totalInitCalls);
  });

  it("lets a later call retry after a failed initialization instead of repeating the same failure forever", async () => {
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "db_select" && String(args?.sql ?? "").includes("PRAGMA user_version")) {
        throw new Error("simulated database initialization failure");
      }
      if (cmd === "db_select") return [];
      if (cmd === "db_execute") return { rowsAffected: 0 };
      if (cmd === "db_execute_batch") return undefined;
      throw new Error(`unexpected invoke call in test: ${cmd}`);
    });
    const { getDb } = await import("@/lib/localDb");

    await expect(getDb()).rejects.toThrow("simulated database initialization failure");

    // Now let a retry succeed, matching what should happen once the
    // transient condition (e.g. the concurrent-transaction race) clears.
    installMigrationFriendlyInvokeMock([]);
    const db = await getDb();
    expect(db).toBeTruthy();
  });
});
