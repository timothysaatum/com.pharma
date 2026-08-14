import { describe, expect, it, vi } from "vitest";
import {
  getCompatibleLocalColumns,
  LEGACY_SYNC_TABLES,
  syncEngine,
} from "@/lib/syncEngine";

describe("sync table routing", () => {
  it("all tables have migrated off the legacy pull; LEGACY_SYNC_TABLES is empty", () => {
    const fullyMigrated = [
      "sales", "drugs", "drug_categories", "price_contracts", "audit_logs",
      "branch_inventory", "drug_batches", "prescriptions", "purchase_orders",
      "customers",
    ];
    for (const table of fullyMigrated) {
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
    engine.pushEvents = vi.fn().mockResolvedValue({ hadFailures: false });
    engine.pull = vi.fn().mockImplementation(async () => { order.push("legacy"); });
    engine.pullEvents = vi.fn().mockResolvedValue(undefined);
    engine.scheduleNetworkRetry = vi.fn();
    engine.logError = vi.fn();

    await engine.sync();

    // CRR push/pull removed — sync order is now: events, reconcile, push, pullEvents.
    // Legacy pull is skipped because LEGACY_SYNC_TABLES is now empty.
    expect(order).toEqual(["reconcile", "push"]);
    expect(engine.pull).not.toHaveBeenCalled();
    engine.branchId = null;
    engine._isSyncing = false;
  });
});
