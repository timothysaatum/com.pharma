import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import {
  applyCustomerMergeDirectivesToDb,
  applyCrrPullChangesToDb,
  type CustomerMergeDirective,
  type Database,
} from "@/lib/localDb";

function params(sql: string): string {
  return sql.replace(/\$(\d+)/g, "?$1");
}

describe("customer merge directives", () => {
  it("atomically repoints references, deletes loser, is idempotent, and repairs delayed rows", async () => {
    const sqlite = new DatabaseSync(":memory:");
    sqlite.exec(`
      CREATE TABLE customers (id TEXT PRIMARY KEY);
      CREATE TABLE sales (id TEXT PRIMARY KEY, customer_id TEXT);
      CREATE TABLE prescriptions (id TEXT PRIMARY KEY, customer_id TEXT);
      CREATE TABLE customer_merge_aliases (
        loser_id TEXT PRIMARY KEY, survivor_id TEXT NOT NULL,
        event_id TEXT NOT NULL, merged_at TEXT NOT NULL
      );
      CREATE TABLE applied_customer_merge_directives (
        event_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL
      );
      CREATE TABLE crsql_changes (
        "table" TEXT, pk TEXT, cid TEXT, val TEXT, col_version INTEGER,
        db_version INTEGER, site_id TEXT, cl INTEGER, seq INTEGER
      );
      INSERT INTO customers VALUES ('winner'), ('loser');
      INSERT INTO sales VALUES ('sale-existing', 'loser');
      INSERT INTO prescriptions VALUES ('rx-existing', 'loser');
    `);
    const db: Database = {
      execute: async (sql, values = []) => {
        const result = sqlite.prepare(params(sql)).run(...(values as any[]));
        return { rowsAffected: Number(result.changes), lastInsertId: Number(result.lastInsertRowid) };
      },
      select: async <T>(sql: string, values: unknown[] = []) =>
        sqlite.prepare(params(sql)).all(...(values as any[])) as T,
      execute_batch: async (sql) => { sqlite.exec(sql); },
      load: async () => db,
    };
    const directive: CustomerMergeDirective = {
      directive_version: 1,
      event_id: "customers:winner:loser",
      survivor_id: "winner",
      loser_id: "loser",
      merged_at: "2026-07-10T12:00:00Z",
    };

    await applyCustomerMergeDirectivesToDb(db, [directive]);
    await applyCustomerMergeDirectivesToDb(db, [directive]);

    expect(sqlite.prepare("SELECT customer_id FROM sales WHERE id='sale-existing'").get()).toEqual({ customer_id: "winner" });
    expect(sqlite.prepare("SELECT customer_id FROM prescriptions WHERE id='rx-existing'").get()).toEqual({ customer_id: "winner" });
    expect(sqlite.prepare("SELECT COUNT(*) count FROM customers WHERE id='loser'").get()).toEqual({ count: 0 });
    expect(sqlite.prepare("SELECT COUNT(*) count FROM applied_customer_merge_directives").get()).toEqual({ count: 1 });

    // A delayed CRR sale can still carry the old ID. The CRR application path
    // normalizes all persisted aliases after inserting remote changes.
    sqlite.exec("INSERT INTO sales VALUES ('sale-delayed', 'loser')");
    await applyCrrPullChangesToDb(db, [{
      table: "sales", pk: "sale-delayed", cid: "customer_id", val: "loser",
      col_version: 1, db_version: 1, site_id: "remote", cl: 1, seq: 1,
    }]);
    expect(sqlite.prepare("SELECT customer_id FROM sales WHERE id='sale-delayed'").get()).toEqual({ customer_id: "winner" });
    expect(sqlite.prepare("SELECT COUNT(*) count FROM applied_customer_merge_directives").get()).toEqual({ count: 1 });
    sqlite.close();
  });
});
