import { describe, expect, it } from "vitest";
import {
  migrate_v15,
  repairIncompleteV15Migration,
  guardAgainstSchemaDowngrade,
  type Database,
} from "@/lib/localDb";

/**
 * Regression coverage for docs/reviews/2026-08-11-offline-first-architecture-review.md:
 * migrate_v15 previously had no transaction wrapper, so a crash between
 * DROP TABLE branch_inventory and the RENAME left a device with neither
 * table — permanently bricking it, since every future launch re-ran the
 * same migration and its INSERT ... FROM branch_inventory failed with
 * "no such table". This suite verifies the fix (transactional rollback)
 * and the one-time repair for devices already stuck in that state.
 */

type Row = Record<string, unknown>;

class MigrationDb {
  tables = new Map<string, Row[]>();
  inTransaction = false;
  committed = false;
  rolledBack = false;
  userVersion = 14;
  failOn: string | null = null;

  constructor(initialBranchInventory: Row[] = []) {
    this.tables.set("branch_inventory", initialBranchInventory);
  }

  async execute(sql: string, _values: unknown[] = []) {
    const normalized = sql.replace(/\s+/g, " ").trim();

    if (this.failOn && normalized.includes(this.failOn)) {
      throw new Error(`simulated failure at: ${this.failOn}`);
    }

    if (normalized === "BEGIN IMMEDIATE") { this.inTransaction = true; return { rowsAffected: 0 }; }
    if (normalized === "COMMIT") { this.inTransaction = false; this.committed = true; return { rowsAffected: 0 }; }
    if (normalized === "ROLLBACK") { this.inTransaction = false; this.rolledBack = true; return { rowsAffected: 0 }; }
    if (normalized === "PRAGMA user_version = 15") { this.userVersion = 15; return { rowsAffected: 0 }; }

    if (normalized.startsWith("CREATE TABLE IF NOT EXISTS branch_inventory_crr")) {
      if (!this.tables.has("branch_inventory_crr")) this.tables.set("branch_inventory_crr", []);
      return { rowsAffected: 0 };
    }
    if (normalized.startsWith("INSERT INTO branch_inventory_crr")) {
      const source = this.tables.get("branch_inventory");
      if (!source) throw new Error("no such table: branch_inventory");
      this.tables.set("branch_inventory_crr", source.map((r) => ({ ...r })));
      return { rowsAffected: source.length };
    }
    if (normalized === "DROP TABLE branch_inventory") {
      if (!this.tables.has("branch_inventory")) throw new Error("no such table: branch_inventory");
      this.tables.delete("branch_inventory");
      return { rowsAffected: 0 };
    }
    if (normalized === "ALTER TABLE branch_inventory_crr RENAME TO branch_inventory") {
      const staged = this.tables.get("branch_inventory_crr");
      if (staged === undefined) throw new Error("no such table: branch_inventory_crr");
      this.tables.set("branch_inventory", staged);
      this.tables.delete("branch_inventory_crr");
      return { rowsAffected: 0 };
    }
    if (normalized.startsWith("INSERT INTO sync_meta")) {
      return { rowsAffected: 1 };
    }
    return { rowsAffected: 0 };
  }

  async execute_batch(_sql: string) {
    // Simulate the extension being available — no-op success.
  }

  async select<T>(sql: string, values: unknown[] = []): Promise<T> {
    const normalized = sql.replace(/\s+/g, " ").trim();
    if (normalized.startsWith("SELECT name FROM sqlite_master")) {
      const name = values[0] as string;
      return (this.tables.has(name) ? [{ name }] : []) as unknown as T;
    }
    return [] as unknown as T;
  }
}

describe("migrate_v15 transactional safety", () => {
  it("commits and lands on user_version=15 on the happy path", async () => {
    const db = new MigrationDb([
      { id: "row-1", branch_id: "b1", drug_id: "d1", quantity: 10, reserved_quantity: 0, updated_at: "2026-01-01" },
    ]);

    await migrate_v15(db as unknown as Database);

    expect(db.committed).toBe(true);
    expect(db.rolledBack).toBe(false);
    expect(db.userVersion).toBe(15);
    expect(db.tables.get("branch_inventory")).toHaveLength(1);
    expect(db.tables.has("branch_inventory_crr")).toBe(false);
  });

  it("rolls back cleanly and leaves branch_inventory intact if the DROP step fails", async () => {
    const db = new MigrationDb([
      { id: "row-1", branch_id: "b1", drug_id: "d1", quantity: 10, reserved_quantity: 0, updated_at: "2026-01-01" },
    ]);
    db.failOn = "DROP TABLE branch_inventory";

    await expect(migrate_v15(db as unknown as Database)).rejects.toThrow();

    expect(db.rolledBack).toBe(true);
    expect(db.committed).toBe(false);
    expect(db.userVersion).toBe(14);
  });

  it("does not brick the device: a retry after a rolled-back failure succeeds normally", async () => {
    const db = new MigrationDb([
      { id: "row-1", branch_id: "b1", drug_id: "d1", quantity: 10, reserved_quantity: 0, updated_at: "2026-01-01" },
    ]);
    db.failOn = "DROP TABLE branch_inventory";
    await expect(migrate_v15(db as unknown as Database)).rejects.toThrow();

    // Real SQLite would have actually rolled back branch_inventory_crr along
    // with everything else in the transaction; our mock doesn't model that
    // automatic rollback of intermediate table state, so clear it manually
    // to represent the real post-ROLLBACK world before retrying.
    db.tables.delete("branch_inventory_crr");
    db.failOn = null;

    await migrate_v15(db as unknown as Database);

    expect(db.userVersion).toBe(15);
    expect(db.tables.get("branch_inventory")).toHaveLength(1);
  });
});

describe("repairIncompleteV15Migration", () => {
  it("renames branch_inventory_crr back to branch_inventory when the device is stuck in the old half-migrated state", async () => {
    const db = new MigrationDb();
    db.tables.delete("branch_inventory");
    db.tables.set("branch_inventory_crr", [{ id: "row-1" }]);

    await repairIncompleteV15Migration(db as unknown as Database, 14);

    expect(db.tables.has("branch_inventory")).toBe(true);
    expect(db.tables.has("branch_inventory_crr")).toBe(false);
  });

  it("is a no-op when branch_inventory already exists", async () => {
    const db = new MigrationDb([{ id: "row-1" }]);

    await repairIncompleteV15Migration(db as unknown as Database, 14);

    expect(db.tables.get("branch_inventory")).toEqual([{ id: "row-1" }]);
  });

  it("is a no-op once the device is already past v15", async () => {
    const db = new MigrationDb();
    db.tables.delete("branch_inventory");
    db.tables.set("branch_inventory_crr", [{ id: "row-1" }]);

    await repairIncompleteV15Migration(db as unknown as Database, 16);

    expect(db.tables.has("branch_inventory")).toBe(false);
  });
});

describe("guardAgainstSchemaDowngrade", () => {
  it("throws when the local schema is newer than this build supports", async () => {
    const db = new MigrationDb();
    await expect(
      guardAgainstSchemaDowngrade(db as unknown as Database, 999)
    ).rejects.toThrow(/newer than this app build/);
  });

  it("does not throw for a known or older schema version", async () => {
    const db = new MigrationDb();
    await expect(guardAgainstSchemaDowngrade(db as unknown as Database, 22)).resolves.toBeUndefined();
    await expect(guardAgainstSchemaDowngrade(db as unknown as Database, 10)).resolves.toBeUndefined();
  });
});
