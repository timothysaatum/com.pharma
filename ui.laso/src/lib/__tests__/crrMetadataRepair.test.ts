import { describe, expect, it } from "vitest";
import {
  ensureCrrMeta,
  ensureCrrTablesEnabled,
  type Database,
} from "@/lib/localDb";

class CrrRepairDb implements Database {
  meta = new Map<string, string>();
  tables = new Set<string>(["purchase_orders"]);
  tracked = false;
  crsqlCalls = 0;

  async execute(sql: string, values: unknown[] = []) {
    const normalized = sql.replace(/\s+/g, " ").trim();
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
    if (normalized.includes("FROM crsql_master")) {
      return [{ count: this.tracked ? 1 : 0 }] as T;
    }
    if (normalized.includes("FROM sqlite_master") && normalized.includes("type = 'trigger'")) {
      return [{ count: this.tracked ? 1 : 0 }] as T;
    }
    return [] as T;
  }

  async execute_batch(sql: string): Promise<void> {
    if (
      sql === "SELECT crsql_as_crr('purchase_orders')"
      || sql === "SELECT crsql_as_crr('sales')"
    ) {
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

    expect(db.meta.get("crr_enabled_purchase_orders")).toBe("0");
  });

  it("repairs false-positive CRR metadata by probing real change tracking", async () => {
    const db = new CrrRepairDb();
    db.meta.set("crr_enabled_purchase_orders", "1");

    await ensureCrrTablesEnabled(db, { strict: false });

    expect(db.crsqlCalls).toBe(1);
    expect(db.meta.get("crr_enabled_purchase_orders")).toBe("1");
  });

  it("recreates a legacy drugs table with CRR-safe defaults before enabling tracking", async () => {
    const db = new CrrRepairDb();
    db.tables = new Set<string>(["drugs"]);
    db.meta.set("crr_enabled_drugs", "0");

    const executed: string[] = [];
    db.execute = async (sql: string, values: unknown[] = []) => {
      const normalized = sql.replace(/\s+/g, " ").trim();
      executed.push(normalized);
      if (normalized.startsWith("INSERT INTO sync_meta")) {
        db.meta.set(String(values[0]), String(values[1]));
        return { rowsAffected: 1 };
      }
      if (normalized === "DELETE FROM sync_meta WHERE key = 'crr_pull_db_version'") {
        db.meta.delete("crr_pull_db_version");
        return { rowsAffected: 1 };
      }
      return { rowsAffected: 0 };
    };
    db.select = async <T>(sql: string, values: unknown[] = []): Promise<T> => {
      const normalized = sql.replace(/\s+/g, " ").trim();
      if (normalized.includes("sqlite_master")) {
        const table = String(values[0]);
        return (db.tables.has(table) ? [{ name: table }] : []) as T;
      }
      if (normalized.includes("FROM sync_meta WHERE key =")) {
        const value = db.meta.get(String(values[0]));
        return (value === undefined ? [] : [{ value }]) as T;
      }
      if (normalized.includes("FROM crsql_master")) {
        return [{ count: db.tracked ? 1 : 0 }] as T;
      }
      if (normalized.includes("FROM sqlite_master") && normalized.includes("type = 'trigger'")) {
        return [{ count: db.tracked ? 1 : 0 }] as T;
      }
      if (normalized === "PRAGMA table_info(drugs)") {
        return [
          { name: "id", notnull: 0, dflt_value: null, pk: 1 },
          { name: "organization_id", notnull: 1, dflt_value: null, pk: 0 },
          { name: "name", notnull: 1, dflt_value: null, pk: 0 },
        ] as T;
      }
      return [] as T;
    };
    db.execute_batch = async (sql: string): Promise<void> => {
      if (sql === "SELECT crsql_as_crr('drugs')") {
        db.crsqlCalls += 1;
        db.tracked = true;
      }
    };

    await ensureCrrTablesEnabled(db, { strict: false });

    expect(executed).toContain("CREATE TABLE drugs_crr_repair ( id TEXT NOT NULL PRIMARY KEY, organization_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', generic_name TEXT, brand_name TEXT, sku TEXT, barcode TEXT, category_id TEXT, drug_type TEXT NOT NULL DEFAULT 'otc', dosage_form TEXT, strength TEXT, manufacturer TEXT, supplier TEXT, requires_prescription INTEGER NOT NULL DEFAULT 0, controlled_substance_schedule TEXT, ndc_code TEXT, unit_price REAL NOT NULL DEFAULT 0, cost_price REAL, markup_percentage REAL, tax_rate REAL NOT NULL DEFAULT 0, reorder_level INTEGER NOT NULL DEFAULT 10, reorder_quantity INTEGER NOT NULL DEFAULT 50, max_stock_level INTEGER, unit_of_measure TEXT NOT NULL DEFAULT 'unit', description TEXT, usage_instructions TEXT, side_effects TEXT, contraindications TEXT, storage_conditions TEXT, image_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, is_deleted INTEGER NOT NULL DEFAULT 0, sync_status TEXT NOT NULL DEFAULT 'synced', sync_version INTEGER NOT NULL DEFAULT 1, synced_at TEXT, updated_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '' )");
    expect(db.crsqlCalls).toBe(1);
    expect(db.meta.get("crr_enabled_drugs")).toBe("1");
  });
});
