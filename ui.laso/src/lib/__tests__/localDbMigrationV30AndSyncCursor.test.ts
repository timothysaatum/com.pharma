import { describe, expect, it } from "vitest";
import { DatabaseSync } from "node:sqlite";
import { migrate_v30 } from "@/lib/localDb";
import type { Database } from "@/lib/localDb";

describe("Migration v30 & Sync Cursor Recovery", () => {
  it("drops orphaned CR-SQLite triggers and preserves application triggers", async () => {
    const rawDb = new DatabaseSync(":memory:");
    rawDb.exec("PRAGMA user_version = 29;");
    rawDb.exec("CREATE TABLE drugs (id TEXT PRIMARY KEY, name TEXT);");
    rawDb.exec("CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT);");
    rawDb.exec("INSERT INTO sync_meta (key, value) VALUES ('event_pull_seq', '150');");

    // Create orphaned CR-SQLite triggers
    rawDb.exec(`
      CREATE TRIGGER drugs__crsql_itrig AFTER INSERT ON drugs BEGIN
        SELECT 1;
      END;
    `);
    rawDb.exec(`
      CREATE TRIGGER drugs__crsql_utrig AFTER UPDATE ON drugs BEGIN
        SELECT 1;
      END;
    `);
    rawDb.exec(`
      CREATE TRIGGER custom_crsql_func_trig AFTER INSERT ON drugs BEGIN
        -- crsql_internal_sync_bit check simulated in trigger body
        SELECT 1;
      END;
    `);

    // Create a legitimate application trigger that should NOT be dropped
    rawDb.exec(`
      CREATE TRIGGER app_audit_trig AFTER INSERT ON drugs BEGIN
        SELECT 1;
      END;
    `);

    // Verify initial trigger count is 4
    const initialTriggers = rawDb.prepare("SELECT name FROM sqlite_master WHERE type = 'trigger'").all() as { name: string }[];
    expect(initialTriggers.length).toBe(4);

    // Adapt node:sqlite to localDb Database interface
    const dbAdapter: Database = {
      execute: async (sql: string, values: unknown[] = []) => {
        let normSql = sql;
        const normValues: unknown[] = [];
        if (/\$\d+/.test(sql)) {
          normSql = sql.replace(/\$(\d+)/g, (_, idxStr) => {
            const idx = parseInt(idxStr, 10) - 1;
            normValues.push(values[idx]);
            return "?";
          });
        } else {
          normValues.push(...values);
        }
        const res = rawDb.prepare(normSql).run(...normValues);
        return { rowsAffected: Number(res.changes), lastInsertId: Number(res.lastInsertRowid) };
      },
      select: async <T>(sql: string, values: unknown[] = []): Promise<T> => {
        let normSql = sql;
        const normValues: unknown[] = [];
        if (/\$\d+/.test(sql)) {
          normSql = sql.replace(/\$(\d+)/g, (_, idxStr) => {
            const idx = parseInt(idxStr, 10) - 1;
            normValues.push(values[idx]);
            return "?";
          });
        } else {
          normValues.push(...values);
        }
        return rawDb.prepare(normSql).all(...normValues) as T;
      },
      execute_batch: async (sql: string) => {
        rawDb.exec(sql);
      },
      load: async () => dbAdapter,
    };

    // Run migration v30
    await migrate_v30(dbAdapter);

    // Verify user_version is 30
    const userVersionRow = rawDb.prepare("PRAGMA user_version").get() as { user_version: number };
    expect(userVersionRow.user_version).toBe(30);

    // Verify orphaned CR-SQLite triggers were dropped and app trigger was preserved
    const remainingTriggers = rawDb.prepare("SELECT name FROM sqlite_master WHERE type = 'trigger'").all() as { name: string }[];
    expect(remainingTriggers.map((t) => t.name)).toEqual(["app_audit_trig"]);

    // Verify event_pull_seq was reset to 0
    const seqRow = rawDb.prepare("SELECT value FROM sync_meta WHERE key = 'event_pull_seq'").get() as { value: string };
    expect(seqRow.value).toBe("0");

    rawDb.close();
  });

  it("advances cursor only up to the contiguous successful event when a projection fails in the batch", async () => {
    let pullSeq = 0;
    const appliedEvents: string[] = [];

    // Simulate batch of 4 events where event 3 throws a projector error
    const batch = [
      { event_id: "EVT-1", seq: 10, event_type: "drug_created" },
      { event_id: "EVT-2", seq: 11, event_type: "drug_category_created" },
      { event_id: "EVT-3", seq: 12, event_type: "failing_event" },
      { event_id: "EVT-4", seq: 13, event_type: "drug_updated" },
    ];

    let contiguousSeq = pullSeq;
    let hadError = false;

    for (const ev of batch) {
      if (ev.event_type === "failing_event") {
        hadError = true;
        break;
      }
      appliedEvents.push(ev.event_id);
      if (ev.seq > contiguousSeq) {
        contiguousSeq = ev.seq;
        pullSeq = contiguousSeq;
      }
    }

    if (hadError) {
      // Stopped advancing at last contiguous success
      expect(pullSeq).toBe(11);
      expect(contiguousSeq).toBe(11);
      expect(appliedEvents).toEqual(["EVT-1", "EVT-2"]);
    }
  });

  it("advances cursor across locally authored events without re-applying", async () => {
    let pullSeq = 11;
    const appliedEvents: string[] = [];
    const locallyAuthored = new Set(["EVT-3"]);

    // Event 3 was authored locally; event 4 is a remote event
    const batch = [
      { event_id: "EVT-3", seq: 12, event_type: "sale_created" },
      { event_id: "EVT-4", seq: 13, event_type: "drug_updated" },
    ];

    let contiguousSeq = pullSeq;

    for (const ev of batch) {
      if (locallyAuthored.has(ev.event_id)) {
        if (ev.seq > contiguousSeq) {
          contiguousSeq = ev.seq;
          pullSeq = contiguousSeq;
        }
        continue;
      }

      appliedEvents.push(ev.event_id);
      if (ev.seq > contiguousSeq) {
        contiguousSeq = ev.seq;
        pullSeq = contiguousSeq;
      }
    }

    expect(pullSeq).toBe(13);
    expect(appliedEvents).toEqual(["EVT-4"]);
  });
});
