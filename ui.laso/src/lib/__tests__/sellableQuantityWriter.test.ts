/**
 * sellableQuantityWriter.test.ts
 *
 * Replaces sellableQuantityProjected.test.ts, which asserted against a value
 * nothing in production ever produced: it mocked db.select to return
 * { quantity: 100, sellable_quantity: 40 } and passed, while every real row sat
 * at the column's DEFAULT 0. A test that mocks the thing under test proves
 * only that the mock was read.
 *
 * These tests run the REAL projector and writer against a REAL in-memory SQLite
 * built by the production migration chain, and assert the persisted column.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  OTHER_BRANCH,
  OTHER_TERMINAL,
  TEST_BRANCH,
  THIS_TERMINAL,
  daysFromNow,
  hoursFromNow,
  insertBatch,
  insertInventory,
  insertLease,
  makeFixture,
  readSellable,
} from "@/lib/__tests__/realDb";
import type { DatabaseSync } from "node:sqlite";

// Route localDb.getDb() at the fixture. runMigrations itself is kept real so
// the schema under test is the production one.
let activeDb: Awaited<ReturnType<typeof makeFixture>>["db"];

vi.mock("@/lib/localDb", async () => {
  const actual = await vi.importActual<typeof import("@/lib/localDb")>("@/lib/localDb");
  return { ...actual, getDb: async () => activeDb };
});

const { refreshSellableQuantity } = await import("@/lib/sellableQty");
const { applyEventLocally } = await import("@/lib/localProjectors");

beforeEach(() => {
  // Pin this device's terminal so lease subtraction is deterministic.
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (k === "laso_terminal_id" ? THIS_TERMINAL : null),
    setItem: () => {},
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("sellable_quantity writer", () => {
  it("populates sellable_quantity from unexpired batches", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 117 });
      insertBatch(r, { remaining: 117 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(117);
  });

  it("falls back to branch_inventory.quantity when the device holds no batches", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 250 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(250);
  });

  it("excludes expired batches but still counts a valid one", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 0 });
      insertBatch(r, { id: "expired", remaining: 90, expiry: daysFromNow(-1) });
      insertBatch(r, { id: "valid", remaining: 27, expiry: daysFromNow(30) });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(27);
  });

  it("reads zero when every batch is expired, without rescuing a stale quantity", async () => {
    // Regression guard: an all-expired drug must NOT fall back to
    // branch_inventory.quantity. Expired stock is not sellable, and a stale
    // aggregate would otherwise resurrect it.
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 250 });
      insertBatch(r, { remaining: 90, expiry: daysFromNow(-30) });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(0);
  });

  it("subtracts an active lease held by another terminal", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 0 });
      insertBatch(r, { remaining: 117 });
      insertLease(r, { terminalId: OTHER_TERMINAL, leased: 40, consumed: 0 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(77);
  });

  it("does not subtract this device's own lease", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 0 });
      insertBatch(r, { remaining: 117 });
      insertLease(r, { terminalId: THIS_TERMINAL, leased: 40, consumed: 0 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(117);
  });

  it("ignores expired and released leases", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 0 });
      insertBatch(r, { remaining: 117 });
      insertLease(r, { id: "l1", terminalId: OTHER_TERMINAL, leased: 30, expiresAt: hoursFromNow(-1) });
      insertLease(r, { id: "l2", terminalId: OTHER_TERMINAL, leased: 30, status: "released" });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(117);
  });

  it("never goes negative", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 0 });
      insertBatch(r, { remaining: 10 });
      insertLease(r, { terminalId: OTHER_TERMINAL, leased: 999 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f");

    expect(readSellable(raw)).toBe(0);
  });

  it("leaves other branches and drugs untouched", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { id: "a", branchId: TEST_BRANCH, drugId: "drug-a", quantity: 10, sellable: 0 });
      insertInventory(r, { id: "b", branchId: OTHER_BRANCH, drugId: "drug-a", quantity: 10, sellable: 0 });
      insertBatch(r, { id: "ba", drugId: "drug-a", remaining: 10 });
    });
    activeDb = db;

    await refreshSellableQuantity(db, TEST_BRANCH, "drug-a");

    expect(readSellable(raw, TEST_BRANCH, "drug-a")).toBe(10);
    expect(readSellable(raw, OTHER_BRANCH, "drug-a")).toBe(0);
  });
});

describe("branch_inventory projector writes sellable_quantity", () => {
  it("branch_inventory_updated with no batches falls back to the payload quantity", async () => {
    const { raw, db } = await makeFixture();
    activeDb = db;

    await applyEventLocally({
      event_id: "e1",
      seq: 1,
      org_id: "org",
      aggregate_id: "inv-geb",
      aggregate_type: "branch_inventory",
      event_type: "branch_inventory_updated",
      schema_version: 1,
      payload: { branch_id: TEST_BRANCH, drug_id: "8d4cc1a7-03c7-4a6a-8080-2bda5def026f", quantity: 117 },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);

    expect(readSellable(raw)).toBe(117);
  });

it("a batch arriving after the inventory row corrects a stale fallback value", async () => {
    // Out-of-order delivery: the inventory row lands first, with no batches
    // present, so sellable_quantity can only fall back to quantity. The batch
    // event lands second and must overwrite that fallback with the real number.
    const { raw, db } = await makeFixture();
    activeDb = db;
    const drugId = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";

    await applyEventLocally({
      event_id: "e1",
      seq: 1,
      org_id: "org",
      aggregate_id: "inv-geb",
      aggregate_type: "branch_inventory",
      event_type: "branch_inventory_created",
      schema_version: 1,
      payload: { branch_id: TEST_BRANCH, drug_id: drugId, quantity: 117 },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);
    expect(readSellable(raw)).toBe(117);

    // Batch says 60 usable out of 100 received. Batches are authoritative, so
    // the fallback of 117 must become 60.
    await applyEventLocally({
      event_id: "e2",
      seq: 2,
      org_id: "org",
      aggregate_id: "batch-geb",
      aggregate_type: "drug_batch",
      event_type: "drug_batch_created",
      schema_version: 1,
      payload: {
        branch_id: TEST_BRANCH,
        drug_id: drugId,
        remaining_quantity: 60,
        quantity: 100,
        batch_number: "CAE32423",
        expiry_date: daysFromNow(400),
      },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);

    expect(readSellable(raw)).toBe(60);
  });

  it("drug_batch_created inserts the inventory row with a correct sellable_quantity", async () => {
    const { raw, db } = await makeFixture();
    activeDb = db;
    const drugId = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";

    await applyEventLocally({
      event_id: "e3",
      seq: 3,
      org_id: "org",
      aggregate_id: "batch-geb",
      aggregate_type: "drug_batch",
      event_type: "drug_batch_created",
      schema_version: 1,
      payload: {
        branch_id: TEST_BRANCH,
        drug_id: drugId,
        remaining_quantity: 123,
        quantity: 123,
        batch_number: "CAE32423",
        expiry_date: daysFromNow(2000),
      },
      dependencies: [],
      authored_at: "2026-01-01T00:00:00Z",
      branch_id: TEST_BRANCH,
      received_at: "2026-01-01T00:00:00Z",
    } as never);

    const row = raw
      .prepare("SELECT quantity, sellable_quantity FROM branch_inventory WHERE drug_id = ?")
      .get(drugId) as { quantity: number; sellable_quantity: number };
    expect(row.quantity).toBe(123);
    expect(row.sellable_quantity).toBe(123);
  });

  it("stock_adjusted recomputes sellable after moving quantity", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { quantity: 100 });
      insertBatch(r, { remaining: 100 });
    });
    activeDb = db;

    await applyEventLocally({
      event_id: "e4",
      seq: 4,
      org_id: "org",
      aggregate_id: "adj-1",
      aggregate_type: "stock_adjustment",
      event_type: "stock_adjusted",
      schema_version: 1,
      payload: {
        branch_id: TEST_BRANCH,
        drug_id: "8d4cc1a7-03c7-4a6a-8080-2bda5def026f",
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
});

describe("v32 backfill", () => {
  it("corrects rows already sitting at the DEFAULT 0", async () => {
    const { raw, db } = await makeFixture((r: DatabaseSync) => {
      insertInventory(r, { id: "old-1", quantity: 117, sellable: 0 });
      insertBatch(r, { remaining: 117 });
      insertInventory(r, { id: "old-2", drugId: "drug-nb", quantity: 42, sellable: 0 });
    });
    activeDb = db;

    const { migrate_v32 } = await import("@/lib/localDb");
    await migrate_v32(db);

    expect(readSellable(raw, TEST_BRANCH, "8d4cc1a7-03c7-4a6a-8080-2bda5def026f")).toBe(117);
    expect(readSellable(raw, TEST_BRANCH, "drug-nb")).toBe(42);
    raw.close();
  });
});