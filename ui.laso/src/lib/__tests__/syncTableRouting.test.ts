import { describe, expect, it, vi } from "vitest";
import {
  getCompatibleLocalColumns,
  LEGACY_SYNC_TABLES,
  syncEngine,
} from "@/lib/syncEngine";

describe("sync table routing", () => {
  it("routes side-effecting sales through legacy protocol-v2 sync only", () => {
    const migrated = [
      "drugs", "drug_categories", "price_contracts", "audit_logs",
      "branch_inventory", "drug_batches", "customers",
      "prescriptions", "purchase_orders",
    ];
    for (const table of migrated) {
      expect(LEGACY_SYNC_TABLES).not.toContain(table);
    }
    expect(LEGACY_SYNC_TABLES).toEqual(["sales"]);
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

  it("repairs the offline queue before push and pulls authoritative sales", async () => {
    const engine = syncEngine as any;
    const order: string[] = [];
    engine.branchId = "branch-edge";
    engine._isSyncing = false;
    engine.reconcileOfflineSales = vi.fn().mockImplementation(async () => { order.push("reconcile"); });
    engine.push = vi.fn().mockImplementation(async () => {
      order.push("push");
      return { nextPullTimestamp: null, hadFailures: false };
    });
    engine.pushCrr = vi.fn().mockImplementation(async () => { order.push("crr-push"); });
    engine.pullCrr = vi.fn().mockImplementation(async () => { order.push("crr"); });
    engine.pull = vi.fn().mockImplementation(async () => { order.push("legacy"); });
    engine.scheduleNetworkRetry = vi.fn();
    engine.logError = vi.fn();

    await engine.sync();

    // crr-push must run BEFORE push so that offline-created prescriptions
    // (table_priority=2 in the batch) land on the server before any sale
    // that references them via prescription_id is pushed.
    expect(order).toEqual(["crr-push", "reconcile", "push", "crr", "legacy"]);
    expect(engine.pullCrr).toHaveBeenCalledOnce();
    expect(engine.pull).toHaveBeenCalledOnce();
    engine.branchId = null;
    engine._isSyncing = false;
  });
});
