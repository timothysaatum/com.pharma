/**
 * sellableQuantityWriter.test.ts
 *
 * Replaces sellableQuantityProjected.test.ts, which asserted against a value
 * nothing in production ever produced: it mocked db.select to return
 * { quantity: 100, sellable_quantity: 40 } and passed, while every real row sat
 * at the column's DEFAULT 0. A test that mocks the thing under test proves
 * only that the mock was read.
 *
 * These run the real projector and writer against a real in-memory SQLite built
 * by the production migration chain, and assert the persisted column.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { DatabaseSync } from "node:sqlite";
import {
  GEBEDOL,
  OTHER_BRANCH,
  OTHER_TERMINAL,
  TEST_BRANCH,
  THIS_TERMINAL,
  daysFromNow,
  hoursFromNow,
  insertBatch,
  insertInventory,
  insertLease,
  installRealDb,
  rawDb,
  readSellable,
  resetTables,
} from "@/lib/__tests__/realDb";

// installRealDb wires the REAL localDb to in-memory SQLite at the Tauri invoke
// boundary, so getDb(), the migration chain, and the projectors all run their
// production code paths against the real schema.
//
// These imports must happen INSIDE beforeAll, after installRealDb() has stubbed
// window. localDb evaluates IS_TAURI at module load, and a top-level import
// would load it before the stub exists, making it resolve to MockDb and turning
// every migration into a silent no-op (no tables, so every insert fails).
let refreshSellableQuantity: typeof import("@/lib/sellableQty").refreshSellableQuantity;
let applyEventLocally: typeof import("@/lib/localProjectors").applyEventLocally;
let getDb: typeof import("@/lib/localDb").getDb;
let migrate_v32: typeof import("@/lib/localDb").migrate_v32;

beforeAll(async () => {
  await installRealDb();
  ({ refreshSellableQuantity } = await import("@/lib/sellableQty"));
  ({ applyEventLocally } = await import("@/lib/localProjectors"));
  ({ getDb, migrate_v32 } = await import("@/lib/localDb"));
});

beforeEach(() => {
  resetTables([
    "drugs",
    "branch_inventory",
    "drug_batches",
    "stock_leases",
    "sync_meta",
    "event_outbox",
  ]);
  // Pin this device's terminal so lease subtraction is deterministic.
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (k === "laso_terminal_id" ? THIS_TERMINAL : null),
    setItem: () => {},
    removeItem: () => {},
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** A branch_inventory_updated envelope for Gebedol at the active branch. */
function inventoryEvent(
  eventId: string,
  seq: number,
  quantity: number,
  eventType: "branch_inventory_created" | "branch_inventory_updated" = "branch_inventory_updated"
) {
  return {
    event_id: eventId,
    seq,
    org_id: "org-1",
    aggregate_id: `agg-${eventId}`,
    aggregate_type: "branch_inventory",
    event_type: eventType,
    schema_version: 1,
    payload: { branch_id: TEST_BRANCH, drug_id: GEBEDOL, quantity },
    dependencies: [],
    authored_at: "2026-01-01T00:00:00Z",
    branch_id: TEST_BRANCH,
    received_at: "2026-01-01T00:00:00Z",
  } as never;
}

/** A drug_batch_created envelope for Gebedol at the active branch. */
function batchEvent(eventId: string, seq: number, remaining: number, expiry: string) {
  return {
    event_id: eventId,
    seq,
    org_id: "org-1",
    aggregate_id: `agg-${eventId}`,
    aggregate_type: "drug_batch",
    event_type: "drug_batch_created",
    schema_version: 1,
    payload: {
      branch_id: TEST_BRANCH,
      drug_id: GEBEDOL,
      remaining_quantity: remaining,
      quantity: remaining + 40,
      batch_number: "CAE32423",
      expiry_date: expiry,
    },
    dependencies: [],
    authored_at: "2026-01-01T00:00:00Z",
    branch_id: TEST_BRANCH,
    received_at: "2026-01-01T00:00:00Z",
  } as never;
}

describe("sellable_quantity writer", () => {
  it("populates sellable_quantity from unexpired batches", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 117 });
    insertBatch(raw, { remaining: 117 });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(117);
  });

  it("falls back to branch_inventory.quantity when the device holds no batches", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 250 });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(250);
  });

  it("excludes expired batches but still counts a valid one", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 0 });
    insertBatch(raw, { id: "expired", remaining: 90, expiry: daysFromNow(-1) });
    insertBatch(raw, { id: "valid", remaining: 27, expiry: daysFromNow(30) });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(27);
  });

  it("reads zero when every batch is expired, without rescuing a stale quantity", async () => {
    // Regression guard: an all-expired drug must NOT fall back to
    // branch_inventory.quantity. Expired stock is not sellable, and a stale
    // aggregate would otherwise resurrect it.
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 250 });
    insertBatch(raw, { remaining: 90, expiry: daysFromNow(-30) });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(0);
  });

  it("subtracts an active lease held by another terminal", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 0 });
    insertBatch(raw, { remaining: 117 });
    insertLease(raw, { terminalId: OTHER_TERMINAL, leased: 40, consumed: 0 });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(77);
  });

  it("does not subtract this device's own lease", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 0 });
    insertBatch(raw, { remaining: 117 });
    insertLease(raw, { terminalId: THIS_TERMINAL, leased: 40, consumed: 0 });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(117);
  });

  it("ignores expired and released leases", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 0 });
    insertBatch(raw, { remaining: 117 });
    insertLease(raw, { id: "l1", terminalId: OTHER_TERMINAL, leased: 30, expiresAt: hoursFromNow(-1) });
    insertLease(raw, { id: "l2", terminalId: OTHER_TERMINAL, leased: 30, status: "released" });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(117);
  });

  it("never goes negative", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 0 });
    insertBatch(raw, { remaining: 10 });
    insertLease(raw, { terminalId: OTHER_TERMINAL, leased: 999 });

    await refreshSellableQuantity(db, TEST_BRANCH, GEBEDOL);

    expect(readSellable(raw)).toBe(0);
  });

  it("leaves other branches and drugs untouched", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { id: "a", branchId: TEST_BRANCH, drugId: "drug-a", quantity: 10, sellable: 0 });
    insertInventory(raw, { id: "b", branchId: OTHER_BRANCH, drugId: "drug-a", quantity: 10, sellable: 0 });
    insertBatch(raw, { id: "ba", drugId: "drug-a", remaining: 10 });

    await refreshSellableQuantity(db, TEST_BRANCH, "drug-a");

    expect(readSellable(raw, TEST_BRANCH, "drug-a")).toBe(10);
    expect(readSellable(raw, OTHER_BRANCH, "drug-a")).toBe(0);
  });
});

describe("branch_inventory projector writes sellable_quantity", () => {
  it("branch_inventory_updated with no batches falls back to the payload quantity", async () => {
    const raw = rawDb();

    await applyEventLocally(inventoryEvent("e1", 1, 117));

    expect(readSellable(raw)).toBe(117);
  });

  it("a batch arriving after the inventory row overwrites the fallback", async () => {
    // Out-of-order delivery: the inventory row lands first with no batches, so
    // sellable can only fall back to quantity. The batch lands second and must
    // replace that fallback with the real number.
    const raw = rawDb();

    await applyEventLocally(inventoryEvent("e1", 1, 117, "branch_inventory_created"));
    expect(readSellable(raw)).toBe(117);

    await applyEventLocally(batchEvent("e2", 2, 60, daysFromNow(400)));
    expect(readSellable(raw)).toBe(60);
  });

  it("drug_batch_created inserts the inventory row with a correct sellable_quantity", async () => {
    const raw = rawDb();

    await applyEventLocally(batchEvent("e3", 3, 123, daysFromNow(2000)));

    const row = raw
      .prepare("SELECT quantity, sellable_quantity FROM branch_inventory WHERE drug_id = ?")
      .get(GEBEDOL) as { quantity: number; sellable_quantity: number };
    expect(row.quantity).toBe(123);
    expect(row.sellable_quantity).toBe(123);
  });

  it("stock_adjusted recomputes sellable after moving quantity", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 100 });
    insertBatch(raw, { remaining: 100 });

    await applyEventLocally({
      event_id: "e4",
      seq: 4,
      org_id: "org-1",
      aggregate_id: "agg-e4",
      aggregate_type: "stock_adjustment",
      event_type: "stock_adjusted",
      schema_version: 1,
      payload: {
        branch_id: TEST_BRANCH,
        drug_id: GEBEDOL,
        quantity_change: -40,
        batch_changes: [{ batch_id: "batch-1", quantity_change: -40 }],
      },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);

    expect(readSellable(raw)).toBe(60);
  });

  it("sale_created recomputes sellable for every deducted drug", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { quantity: 117 });
    insertBatch(raw, { id: "b1", remaining: 117 });

    await applyEventLocally({
      event_id: "e5",
      seq: 5,
      org_id: "org-1",
      aggregate_id: "agg-sale",
      aggregate_type: "sale",
      event_type: "sale_created",
      schema_version: 1,
      payload: {
        branch_id: TEST_BRANCH,
        organization_id: "org-1",
        sale_number: "S-1",
        cashier_id: "u1",
        items: [{ drug_id: GEBEDOL, quantity: 17, unit_price: 5, quantity_used: 17, total: 85 }],
        // The sale deducts the batch and the aggregate. Both must be reflected.
        batch_changes: [{ batch_id: "b1", quantity_used: 17 }],
        subtotal: 85,
        tax_amount: 0,
        total_amount: 85,
      },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);

    const row = raw
      .prepare("SELECT quantity, sellable_quantity FROM branch_inventory WHERE drug_id = ?")
      .get(GEBEDOL) as { quantity: number; sellable_quantity: number };
    expect(row.quantity).toBe(100);
    expect(row.sellable_quantity).toBe(100);
  });
});

describe("v32 backfill", () => {
  it("corrects rows already sitting at the DEFAULT 0", async () => {
    const db = await getDb();
    const raw = rawDb();
    insertInventory(raw, { id: "old-1", quantity: 117, sellable: 0 });
    insertBatch(raw, { remaining: 117 });
    insertInventory(raw, { id: "old-2", drugId: "drug-nb", quantity: 42, sellable: 0 });

    await migrate_v32(db);

    expect(readSellable(raw, TEST_BRANCH, GEBEDOL)).toBe(117);
    expect(readSellable(raw, TEST_BRANCH, "drug-nb")).toBe(42);
  });
});