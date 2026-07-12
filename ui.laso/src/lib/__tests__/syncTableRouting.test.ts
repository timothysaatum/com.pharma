import { describe, expect, it, vi } from "vitest";
import {
  getCompatibleLocalColumns,
  LEGACY_SYNC_TABLES,
  syncEngine,
} from "@/lib/syncEngine";

describe("sync table routing", () => {
  it("does not request CRR-migrated tables through legacy pull", () => {
    const migrated = [
      "drugs", "drug_categories", "price_contracts", "audit_logs",
      "branch_inventory", "drug_batches", "customers",
      "prescriptions", "purchase_orders", "sales",
    ];
    for (const table of migrated) {
      expect(LEGACY_SYNC_TABLES).not.toContain(table);
    }
    expect(LEGACY_SYNC_TABLES).toEqual([]);
  });

  it("drops additive server fields missing from an older local schema", async () => {
    const db = {
      select: async () => [
        { name: "id" }, { name: "user_id" }, { name: "action" },
      ],
    };
    const columns = await getCompatibleLocalColumns(
      db as any,
      "audit_logs",
      ["id", "user_id", "user_full_name", "action"],
    );
    expect(columns).toEqual(["id", "user_id", "action"]);
  });

  it("runs CRR pull without invoking legacy pull when no legacy tables remain", async () => {
    const engine = syncEngine as any;
    const order: string[] = [];
    engine.branchId = "branch-edge";
    engine._isSyncing = false;
    engine.push = vi.fn().mockResolvedValue({ nextPullTimestamp: null, hadFailures: false });
    engine.pushCrr = vi.fn().mockResolvedValue(undefined);
    engine.reconcileOfflineSales = vi.fn().mockResolvedValue(undefined);
    engine.pullCrr = vi.fn().mockImplementation(async () => { order.push("crr"); });
    engine.pull = vi.fn().mockImplementation(async () => { order.push("legacy"); });
    engine.scheduleNetworkRetry = vi.fn();
    engine.logError = vi.fn();

    await engine.sync();

    expect(order).toEqual(["crr"]);
    expect(engine.pullCrr).toHaveBeenCalledOnce();
    expect(engine.pull).not.toHaveBeenCalled();
    engine.branchId = null;
    engine._isSyncing = false;
  });
});
