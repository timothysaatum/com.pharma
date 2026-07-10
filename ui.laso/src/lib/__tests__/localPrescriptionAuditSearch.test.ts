import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it, vi } from "vitest";

function numberedParams(sql: string): string {
  return sql.replace(/\$(\d+)/g, "?$1");
}

describe("offline prescription historical-number search", () => {
  afterEach(() => {
    vi.doUnmock("@/lib/localDb");
    vi.resetModules();
  });

  it("finds a renamed row through crr_renumber_audit and returns annotation metadata", async () => {
    const sqlite = new DatabaseSync(":memory:");
    sqlite.exec(`
      CREATE TABLE customers (id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT);
      CREATE TABLE prescriptions (
        id TEXT PRIMARY KEY, organization_id TEXT, branch_id TEXT,
        prescription_number TEXT, customer_id TEXT, prescriber_name TEXT,
        medications TEXT, refills_allowed INTEGER, refills_remaining INTEGER,
        status TEXT, expiry_date TEXT, created_at TEXT
      );
      CREATE TABLE crr_renumber_audit (
        id INTEGER PRIMARY KEY, table_name TEXT, winner_id TEXT, loser_id TEXT,
        old_business_key TEXT, new_business_key TEXT, renumbered_at TEXT
      );
      INSERT INTO customers VALUES ('customer-1', 'Ada', 'Patient');
      INSERT INTO prescriptions VALUES (
        'rx-loser', 'org-1', 'branch-1', 'RX-7-C', 'customer-1', 'Dr Example',
        '[]', 2, 2, 'active', '2027-01-01', '2026-01-02'
      );
      INSERT INTO crr_renumber_audit VALUES (
        1, 'prescriptions', 'rx-winner', 'rx-loser',
        'RX-7', 'RX-7-C', '2026-01-02T03:04:05Z'
      );
    `);

    const adapter = {
      select: async <T>(sql: string, values: unknown[] = []): Promise<T> =>
        sqlite.prepare(numberedParams(sql)).all(...(values as any[])) as T,
    };
    vi.doMock("@/lib/localDb", () => ({ getDb: async () => adapter }));
    const { localRead } = await import("@/lib/localRead");

    const result = await localRead.searchPrescriptions({
      organization_id: "org-1",
      branch_id: "branch-1",
      status_filter: "active",
      include_expired: true,
      search: "RX-7",
    });

    expect(result.total).toBe(1);
    expect(result.items[0]).toMatchObject({
      id: "rx-loser",
      prescription_number: "RX-7-C",
      renumbered_from: "RX-7",
      renumbered_to: "RX-7-C",
      renumbered_at: "2026-01-02T03:04:05Z",
      collision_survivor_id: "rx-winner",
    });
    sqlite.close();
  });
});
