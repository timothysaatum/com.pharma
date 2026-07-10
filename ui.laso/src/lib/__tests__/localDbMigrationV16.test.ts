import { describe, expect, it } from "vitest";
import { migrate_v16 } from "@/lib/localDb";

type Row = Record<string, unknown>;

class MigrationDb {
  tables = new Map<string, Row[]>();
  audit: Row[] = [];
  inTransaction = false;
  userVersion = 15;

  constructor() {
    this.tables.set("drug_batches", []);
    this.tables.set("customers", []);
    this.tables.set("purchase_orders", []);
    this.tables.set("prescriptions", [
      { id: "rx-1", organization_id: "org-1", prescription_number: "RX-7", created_at: "2026-01-01" },
      { id: "rx-2", organization_id: "org-1", prescription_number: "RX-7", created_at: "2026-01-02" },
      { id: "rx-3", organization_id: "org-1", prescription_number: "RX-7", created_at: "2026-01-03" },
      { id: "rx-4", organization_id: "org-1", prescription_number: "RX-7-B", created_at: "2025-12-01" },
      { id: "rx-5", organization_id: "org-2", prescription_number: "RX-7", created_at: "2026-01-04" },
    ]);
    this.tables.set("sales", [
      { id: "sale-1", branch_id: "branch-1", sale_number: "SALE-9", created_at: "2026-02-01" },
      { id: "sale-2", branch_id: "branch-1", sale_number: "SALE-9", created_at: "2026-02-02" },
      { id: "sale-3", branch_id: "branch-1", sale_number: "SALE-9", created_at: "2026-02-03" },
      { id: "sale-4", branch_id: "branch-2", sale_number: "SALE-9", created_at: "2026-02-04" },
    ]);
  }

  async execute(sql: string, values: unknown[] = []) {
    const normalized = sql.replace(/\s+/g, " ").trim();
    if (normalized === "BEGIN IMMEDIATE") this.inTransaction = true;
    if (normalized === "COMMIT" || normalized === "ROLLBACK") this.inTransaction = false;
    if (normalized === "PRAGMA user_version = 16") this.userVersion = 16;

    const update = normalized.match(/^UPDATE (\w+) SET (\w+) = \$1 WHERE id = \$2$/);
    if (update) {
      const [, table, column] = update;
      const row = this.tables.get(table)?.find((candidate) => candidate.id === values[1]);
      if (row) row[column] = values[0];
    }

    if (normalized.startsWith("INSERT INTO crr_renumber_audit")) {
      const [event_id, table_name, winner_id, loser_id, business_key_col, old_business_key, new_business_key] = values;
      this.audit.push({ event_id, table_name, winner_id, loser_id, business_key_col, old_business_key, new_business_key });
    }

    const copy = normalized.match(/^INSERT INTO (\w+)_crr \(.+\) SELECT .+ FROM (\w+)$/);
    if (copy) {
      this.tables.set(`${copy[1]}_crr`, (this.tables.get(copy[2]) ?? []).map((row) => ({ ...row })));
    }

    const drop = normalized.match(/^DROP TABLE (\w+)$/);
    if (drop) this.tables.delete(drop[1]);

    const rename = normalized.match(/^ALTER TABLE (\w+)_crr RENAME TO (\w+)$/);
    if (rename) {
      this.tables.set(rename[2], this.tables.get(`${rename[1]}_crr`) ?? []);
      this.tables.delete(`${rename[1]}_crr`);
    }

    return { rowsAffected: 0 };
  }

  async select<T>(sql: string): Promise<T> {
    const normalized = sql.replace(/\s+/g, " ").trim();
    const match = normalized.match(
      /^SELECT id, (\w+) AS scope_value, (\w+) AS business_key, created_at FROM (\w+) ORDER BY/
    );
    if (!match) return [] as T;
    const [, scopeColumn, keyColumn, table] = match;
    const result = (this.tables.get(table) ?? [])
      .map((row) => ({
        id: row.id,
        scope_value: row[scopeColumn],
        business_key: row[keyColumn],
        created_at: row.created_at,
      }))
      .sort((a, b) => JSON.stringify([a.scope_value, a.business_key, a.created_at, a.id])
        .localeCompare(JSON.stringify([b.scope_value, b.business_key, b.created_at, b.id])));
    return result as T;
  }

  async execute_batch() {}
  async load() { return this; }
}

describe("migration v16 keep_both_renumber", () => {
  it("preserves every prescription and sale row and audits each collision rename", async () => {
    const db = new MigrationDb();
    const prescriptionCountBefore = db.tables.get("prescriptions")!.length;
    const salesCountBefore = db.tables.get("sales")!.length;

    // The removed ROW_NUMBER(... WHERE rn = 1) implementation would have kept
    // only 3/5 prescriptions and 2/4 sales in this deliberate-collision fixture.
    const legacyPrescriptionCount = 3;
    const legacySalesCount = 2;

    await migrate_v16(db as never);

    const prescriptions = db.tables.get("prescriptions")!;
    const sales = db.tables.get("sales")!;
    expect([legacyPrescriptionCount, prescriptionCountBefore, prescriptions.length]).toEqual([3, 5, 5]);
    expect([legacySalesCount, salesCountBefore, sales.length]).toEqual([2, 4, 4]);

    expect(prescriptions.map((row) => [row.id, row.prescription_number])).toEqual([
      ["rx-1", "RX-7"],
      ["rx-2", "RX-7-C"],
      ["rx-3", "RX-7-D"],
      ["rx-4", "RX-7-B"],
      ["rx-5", "RX-7"],
    ]);
    expect(sales.map((row) => [row.id, row.sale_number])).toEqual([
      ["sale-1", "SALE-9"],
      ["sale-2", "SALE-9-B"],
      ["sale-3", "SALE-9-C"],
      ["sale-4", "SALE-9"],
    ]);
    expect(db.audit).toHaveLength(4);
    expect(db.audit).toContainEqual({
      event_id: "prescriptions:rx-2:RX-7:RX-7-C",
      table_name: "prescriptions",
      winner_id: "rx-1",
      loser_id: "rx-2",
      business_key_col: "prescription_number",
      old_business_key: "RX-7",
      new_business_key: "RX-7-C",
    });
    expect(db.userVersion).toBe(16);
    expect(db.inTransaction).toBe(false);
  });
});
