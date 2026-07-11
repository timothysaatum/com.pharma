import { describe, expect, it } from "vitest";
import { LEGACY_SYNC_TABLES } from "@/lib/syncEngine";

describe("sync table routing", () => {
  it("does not request CRR-migrated tables through legacy pull", () => {
    const migrated = [
      "branch_inventory", "drug_batches", "customers",
      "prescriptions", "purchase_orders", "sales",
    ];
    for (const table of migrated) {
      expect(LEGACY_SYNC_TABLES).not.toContain(table);
    }
    expect(LEGACY_SYNC_TABLES).toEqual([
      "drugs", "drug_categories", "price_contracts", "audit_logs",
    ]);
  });
});
