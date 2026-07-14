import { describe, expect, it } from "vitest";
import {
  ensureCrrMeta,
  ensureCrrTablesEnabled,
  type Database,
} from "@/lib/localDb";

class CrrRepairDb implements Database {
  meta = new Map<string, string>();
  tables = new Set<string>(["sales"]);
  tracked = false;
  probeChanges = 0;
  inProbe = false;
  crsqlCalls = 0;

  async execute(sql: string, values: unknown[] = []) {
    const normalized = sql.replace(/\s+/g, " ").trim();
    if (normalized === "SAVEPOINT crr_tracking_probe") {
      this.inProbe = true;
      this.probeChanges = 0;
      return { rowsAffected: 0 };
    }
    if (
      normalized === "ROLLBACK TO crr_tracking_probe"
      || normalized === "RELEASE crr_tracking_probe"
    ) {
      this.inProbe = false;
      this.probeChanges = 0;
      return { rowsAffected: 0 };
    }
    if (normalized.startsWith("INSERT INTO sales (id)")) {
      if (this.tracked && this.inProbe) this.probeChanges += 1;
      return { rowsAffected: 1 };
    }
    if (normalized.startsWith("INSERT INTO sync_meta")) {
      this.meta.set(String(values[0]), String(values[1]));
      return { rowsAffected: 1 };
    }
    if (normalized === "DELETE FROM sync_meta WHERE key = 'crr_pull_db_version'") {
      this.meta.delete("crr_pull_db_version");
      return { rowsAffected: 1 };
    }
    return { rowsAffected: 0 };
  }

  async select<T>(sql: string, values: unknown[] = []): Promise<T> {
    const normalized = sql.replace(/\s+/g, " ").trim();
    if (normalized.includes("sqlite_master")) {
      const table = String(values[0]);
      return (this.tables.has(table) ? [{ name: table }] : []) as T;
    }
    if (normalized.includes("FROM sync_meta WHERE key =")) {
      const value = this.meta.get(String(values[0]));
      return (value === undefined ? [] : [{ value }]) as T;
    }
    if (normalized.includes("FROM crsql_changes")) {
      return [{ count: this.probeChanges }] as T;
    }
    return [] as T;
  }

  async execute_batch(sql: string): Promise<void> {
    if (sql === "SELECT crsql_as_crr('sales')") {
      this.crsqlCalls += 1;
      this.tracked = true;
    }
  }

  async load(): Promise<Database> {
    return this;
  }
}

describe("CRR metadata repair", () => {
  it("does not seed missing CRR metadata as enabled before verification", async () => {
    const db = new CrrRepairDb();

    await ensureCrrMeta(db);

    expect(db.meta.get("crr_enabled_sales")).toBe("0");
  });

  it("repairs false-positive CRR metadata by probing real change tracking", async () => {
    const db = new CrrRepairDb();
    db.meta.set("crr_enabled_sales", "1");

    await ensureCrrTablesEnabled(db, { strict: false });

    expect(db.crsqlCalls).toBe(1);
    expect(db.meta.get("crr_enabled_sales")).toBe("1");
  });
});
