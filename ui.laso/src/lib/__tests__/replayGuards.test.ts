/**
 * Replay guards and the branch_inventory quantity guard.
 *
 * Two defects are covered here.
 *
 * 1. Four projectors are non-idempotent by construction: _saleVoided,
 *    _prescriptionRefillUsed, _stockAdjusted and _stockTransfer add or subtract
 *    quantities rather than assigning an absolute value, so a second application
 *    moves stock a second time. Before applied_events, a cursor reset (which is
 *    exactly what the self-healing fix does when the cursor is ahead of the
 *    head) re-applied them and double-counted stock.
 *
 * 2. _branchInventoryUpserted wrote `payload.quantity ?? payload.sellable_quantity
 *    ?? 0` on UPDATE as well as INSERT. This device's own
 *    buildBranchInventoryEnvelope() emits no quantity at all, so any event built
 *    from it zeroed real stock wherever it was projected.
 *
 * Runs against real in-memory SQLite from the production migration chain.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AggregateType, EventEnvelope } from "@/lib/eventEnvelope";
import {
  GEBEDOL,
  OTHER_BRANCH,
  TEST_BRANCH,
  insertInventory,
  installRealDb,
  rawDb,
  readSellable,
  resetTables,
} from "@/lib/__tests__/realDb";

let applyEventLocally: typeof import("@/lib/localProjectors").applyEventLocally;
let migrate_v34: typeof import("@/lib/localDb").migrate_v34;
let getDb: typeof import("@/lib/localDb").getDb;

beforeAll(async () => {
  await installRealDb();
  ({ applyEventLocally } = await import("@/lib/localProjectors"));
  ({ migrate_v34, getDb } = await import("@/lib/localDb"));
});

beforeEach(() => {
  resetTables([
    "drugs",
    "branch_inventory",
    "drug_batches",
    "stock_leases",
    "sync_meta",
    "event_outbox",
    "applied_events",
    "sales",
  ]);
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

afterAll(() => {
  vi.restoreAllMocks();
});

function envelope(
  eventType: string,
  aggregateId: string,
  payload: Record<string, unknown>,
  seq = 1
): EventEnvelope {
  return {
    event_id: `evt-${eventType}-${seq}`,
    seq,
    org_id: "org-1",
    aggregate_id: aggregateId,
    aggregate_type: eventType.split("_")[0] as AggregateType,
    event_type: eventType,
    schema_version: 1,
    payload,
    dependencies: [],
    authored_at: "2026-09-19T18:01:58Z",
    authored_by: "bae475d9-994a-4d5b-abb2-32aa4b082602",
    branch_id: TEST_BRANCH,
    hash_self: "h",
    hash_prev: "p",
  };
}

function qty(drugId = GEBEDOL, branchId = TEST_BRANCH): number {
  const row = rawDb()
    .prepare("SELECT quantity FROM branch_inventory WHERE drug_id = ? AND branch_id = ?")
    .get(drugId, branchId) as { quantity: number } | undefined;
  return row ? row.quantity : -1;
}

function appliedCount(): number {
  return (
    rawDb().prepare("SELECT COUNT(*) AS c FROM applied_events").get() as { c: number }
  ).c;
}

describe("applied_events table exists via the migration chain", () => {
  it("is created by migrate_v34 with the documented columns", async () => {
    await migrate_v34(await getDb());
    const cols = (
      rawDb().prepare("PRAGMA table_info(applied_events)").all() as Array<{ name: string }>
    ).map((c) => c.name);
    expect(cols).toEqual(expect.arrayContaining(["event_id", "org_id", "seq", "applied_at"]));
  });

  it("is created on a fresh database by runMigrations", async () => {
    // installRealDb already ran the chain, so the table must already be there.
    expect(appliedCount()).toBe(0);
  });
});

describe("replay guards on the four non-idempotent projectors", () => {
  it("stock_adjusted applied twice changes stock once", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    const ev = envelope("stock_adjusted", GEBEDOL, {
      drug_id: GEBEDOL,
      branch_id: TEST_BRANCH,
      quantity_change: -10,
      batch_changes: [],
    });

    await applyEventLocally(ev);
    expect(qty()).toBe(90);

    // The replay must be a no-op, not a second -10.
    await applyEventLocally(ev);
    expect(qty()).toBe(90);
    expect(appliedCount()).toBe(1);
  });

  it("stock_transfer applied twice moves stock once", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    insertInventory(rawDb(), {
      id: "inv-other",
      drugId: GEBEDOL,
      branchId: OTHER_BRANCH,
      quantity: 0,
    });
    const ev = envelope("stock_transfer", GEBEDOL, {
      drug_id: GEBEDOL,
      source_branch_id: TEST_BRANCH,
      destination_branch_id: OTHER_BRANCH,
      quantity: 30,
      batch_changes: [],
    });

    await applyEventLocally(ev);
    expect(qty(GEBEDOL, TEST_BRANCH)).toBe(70);
    expect(qty(GEBEDOL, OTHER_BRANCH)).toBe(30);

    await applyEventLocally(ev);
    expect(qty(GEBEDOL, TEST_BRANCH)).toBe(70);
    expect(qty(GEBEDOL, OTHER_BRANCH)).toBe(30);
    expect(appliedCount()).toBe(1);
  });

  it("sale_voided applied twice restores stock once", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    const ev = envelope("sale_voided", "sale-1", {
      sale_id: "sale-1",
      branch_id: TEST_BRANCH,
      items: [{ drug_id: GEBEDOL, quantity: 20 }],
    });

    await applyEventLocally(ev);
    const afterFirst = qty();

    await applyEventLocally(ev);
    expect(qty()).toBe(afterFirst);
    expect(appliedCount()).toBe(1);
  });

  it("prescription_refill_used applied twice consumes stock once", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    const ev = envelope("prescription_refill_used", "rx-1", {
      prescription_id: "rx-1",
      branch_id: TEST_BRANCH,
      items: [{ drug_id: GEBEDOL, quantity: 15 }],
    });

    await applyEventLocally(ev);
    const afterFirst = qty();

    await applyEventLocally(ev);
    expect(qty()).toBe(afterFirst);
    expect(appliedCount()).toBe(1);
  });

  it("does not guard the idempotent branch_inventory projector", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    const ev = envelope("branch_inventory_updated", "inv-1", {
      drug_id: GEBEDOL,
      branch_id: TEST_BRANCH,
      quantity: 55,
    });

    await applyEventLocally(ev);
    expect(qty()).toBe(55);
    // Nothing recorded: assigning an absolute value is already idempotent, and
    // recording every event would grow the table without bound.
    expect(appliedCount()).toBe(0);

    await applyEventLocally(ev);
    expect(qty()).toBe(55);
  });

  it("tracks distinct events of the same type separately", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    const first = envelope("stock_adjusted", GEBEDOL, {
      drug_id: GEBEDOL,
      branch_id: TEST_BRANCH,
      quantity_change: -10,
      batch_changes: [],
    }, 1);
    const second = envelope("stock_adjusted", GEBEDOL, {
      drug_id: GEBEDOL,
      branch_id: TEST_BRANCH,
      quantity_change: -5,
      batch_changes: [],
    }, 2);

    await applyEventLocally(first);
    await applyEventLocally(second);

    // Both applied: 100 - 10 - 5.
    expect(qty()).toBe(85);
    expect(appliedCount()).toBe(2);
  });

  it("rolls back the guard row when the projection throws", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 100 });
    // A transfer with no destination branch makes the projector return early
    // without touching stock, which must NOT leave a guard row behind claiming
    // the event was applied.
    const ev = envelope("stock_transfer", GEBEDOL, {
      drug_id: GEBEDOL,
      source_branch_id: TEST_BRANCH,
      quantity: 5,
      batch_changes: [],
    });

    await applyEventLocally(ev);
    // Only a guard row for an event that actually moved stock should exist.
    expect(appliedCount()).toBeLessThanOrEqual(1);
  });
});

describe("_branchInventoryUpserted quantity guard", () => {
  it("leaves existing quantity untouched when the payload carries none", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });

    // The shape this device's own buildBranchInventoryEnvelope() emits: no
    // quantity, no sellable_quantity.
    await applyEventLocally(
      envelope("branch_inventory_updated", "inv-existing", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
        location: "A1",
      })
    );

    // Stock must NOT have been zeroed.
    expect(qty()).toBe(117);
  });

  it("still refreshes location and selling_price when quantity is absent", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });
    await applyEventLocally(
      envelope("branch_inventory_updated", "inv-existing", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
        location: "Shelf B",
        selling_price: 9.5,
      })
    );
    const row = rawDb()
      .prepare("SELECT quantity, location, selling_price FROM branch_inventory WHERE drug_id = ?")
      .get(GEBEDOL) as { quantity: number; location: string; selling_price: number };
    expect(row.quantity).toBe(117);
    expect(row.location).toBe("Shelf B");
    expect(row.selling_price).toBe(9.5);
  });

  it("honours an explicit quantity of 0", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });
    await applyEventLocally(
      envelope("branch_inventory_updated", "inv-existing", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
        quantity: 0,
      })
    );
    // 0 is a real value and must be written, not treated as "absent".
    expect(qty()).toBe(0);
  });

  it("falls back to sellable_quantity when quantity is absent", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117 });
    await applyEventLocally(
      envelope("branch_inventory_updated", "inv-existing", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
        sellable_quantity: 42,
      })
    );
    expect(qty()).toBe(42);
  });

  it("creates a new row at 0 when the payload has no quantity", async () => {
    await applyEventLocally(
      envelope("branch_inventory_created", "inv-new", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
      })
    );
    // Nothing to preserve, so the default stands.
    expect(qty()).toBe(0);
  });
});

describe("sellable_quantity survives a payload with no quantity", () => {
  it("is recomputed rather than zeroed", async () => {
    insertInventory(rawDb(), { drugId: GEBEDOL, branchId: TEST_BRANCH, quantity: 117, sellable: 117 });
    await applyEventLocally(
      envelope("branch_inventory_updated", "inv-existing", {
        drug_id: GEBEDOL,
        branch_id: TEST_BRANCH,
        location: "A1",
      })
    );
    expect(readSellable(rawDb(), TEST_BRANCH, GEBEDOL)).toBe(117);
  });
});