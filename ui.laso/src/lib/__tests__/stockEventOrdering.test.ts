/**
 * Stock events must be order-INDEPENDENT (C-hybrid).
 *
 * ORIGINAL PURPOSE, KEPT AS HISTORY. This file was ported from
 * origin/fix/stock-sync, where it existed to determine which emission ORDER made
 * a device's branch_inventory match the server. Its finding was that the two
 * stock projectors contradicted each other:
 *
 *   _branchInventoryUpserted  ASSIGNED quantity = payload.quantity  (absolute)
 *   _drugBatchUpserted        ADDED    quantity += remaining_quantity on create
 *
 * so the same event set gave a different answer per order. branch_inventory
 * first then overshot: 117 -> 234, and 247 -> 494. The conclusion that branch
 * was to prescribe "batches first, inventory last".
 *
 * WHY THAT IS NO LONGER THE DESIGN. Prescribing an order only works when the
 * whole set is delivered in one ordered pass. It does not cover:
 *
 *   - a cursor rewind, which replays the log in seq order regardless of
 *     whichever order the emitter chose;
 *   - a device that already holds the branch_inventory row receiving a batch
 *     event later, which inflated 247 to 347.
 *
 * branch_inventory.quantity is now DERIVED from the local drug_batches rows
 * wherever any exist (recomputeQuantityFromBatches, mirroring the server's
 * _recalculate_inventory_quantity), and the batch projector no longer writes
 * quantity at all. One writer, so delivery order cannot matter.
 *
 * The two tests that asserted 234 and 494 as the EXPECTED outcome are rewritten
 * below to assert the same server quantity in both orders. That inversion is the
 * record that the defect is gone.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { EventEnvelope } from "@/lib/eventEnvelope";
import {
  GEBEDOL,
  OTHER_BRANCH,
  TEST_BRANCH,
  insertBatch,
  insertInventory,
  installRealDb,
  rawDb,
  resetTables,
} from "@/lib/__tests__/realDb";

let applyEventLocally: typeof import("@/lib/localProjectors").applyEventLocally;

const AMOX_ID = "9038e0e0-459f-4a6f-8bd1-72bb2b8447e0";
const ORG = "org-1";
const AUTHOR = "bae475d9-994a-4d5b-abb2-32aa4b082602";

beforeAll(async () => {
  await installRealDb();
  ({ applyEventLocally } = await import("@/lib/localProjectors"));
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
    "sync_event_failures",
  ]);
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {} });
});

afterAll(() => vi.restoreAllMocks());

let seqCounter = 0;

function biEvent(
  aggregateId: string,
  drugId: string,
  quantity: number,
  sellingPrice = 5
): EventEnvelope {
  seqCounter += 1;
  return {
    event_id: `evt-bi-${aggregateId}-${seqCounter}`,
    seq: seqCounter,
    org_id: ORG,
    aggregate_id: aggregateId,
    aggregate_type: "branch_inventory",
    event_type: "branch_inventory_created",
    schema_version: 1,
    payload: {
      branch_inventory_id: aggregateId,
      branch_id: TEST_BRANCH,
      drug_id: drugId,
      quantity,
      reserved_quantity: 0,
      location: "A1",
      selling_price: sellingPrice,
    },
    dependencies: [],
    authored_at: "2026-09-19T18:01:58Z",
    authored_by: AUTHOR,
    branch_id: TEST_BRANCH,
    hash_self: "a".repeat(64),
    hash_prev: "b".repeat(64),
  };
}

function batchEvent(
  batchId: string,
  drugId: string,
  remaining: number,
  quantity = remaining
): EventEnvelope {
  seqCounter += 1;
  return {
    event_id: `evt-batch-${batchId}-${seqCounter}`,
    seq: seqCounter,
    org_id: ORG,
    aggregate_id: batchId,
    aggregate_type: "drug_batch",
    event_type: "drug_batch_created",
    schema_version: 1,
    payload: {
      id: batchId,
      batch_id: batchId,
      drug_id: drugId,
      branch_id: TEST_BRANCH,
      batch_number: `LOT-${batchId.slice(0, 4)}`,
      quantity,
      remaining_quantity: remaining,
      cost_price: 3,
      selling_price: 5,
      expiry_date: "2032-09-19",
      supplier: "Apomuden",
      purchase_order_id: null,
    },
    dependencies: [],
    authored_at: "2026-09-19T18:01:58Z",
    authored_by: AUTHOR,
    branch_id: TEST_BRANCH,
    hash_self: "a".repeat(64),
    hash_prev: "b".repeat(64),
  };
}

function qty(drugId: string, branchId = TEST_BRANCH): number {
  const row = rawDb()
    .prepare("SELECT quantity FROM branch_inventory WHERE drug_id = ? AND branch_id = ?")
    .get(drugId, branchId) as { quantity: number } | undefined;
  return row ? row.quantity : -1;
}

function batchRemaining(batchId: string): number {
  const row = rawDb()
    .prepare("SELECT remaining_quantity FROM drug_batches WHERE id = ?")
    .get(batchId) as { remaining_quantity: number } | undefined;
  return row ? row.remaining_quantity : -1;
}

describe("single batch: which emission order reproduces the server quantity", () => {
  it("batch FIRST then branch_inventory: device quantity equals the server", async () => {
    const batch = "11111111-1111-1111-1111-111111111111";
    const inv = "22222222-2222-2222-2222-222222222222";

    await applyEventLocally(batchEvent(batch, GEBEDOL, 117));
    await applyEventLocally(biEvent(inv, GEBEDOL, 117));

    // Server: branch_inventory.quantity = 117
    expect(qty(GEBEDOL)).toBe(117);
    expect(batchRemaining(batch)).toBe(117);
  });

  it("branch_inventory FIRST then batch: device quantity is now ALSO 117", async () => {
    const batch = "33333333-3333-3333-3333-333333333333";
    const inv = "44444444-4444-4444-4444-444444444444";

    await applyEventLocally(biEvent(inv, GEBEDOL, 117));
    await applyEventLocally(batchEvent(batch, GEBEDOL, 117));

    // Previously 234. The batch projector no longer adds to quantity: it writes
    // the batch row, then quantity is derived from it. The absolute 117 in the
    // payload is a snapshot and is replaced by the live batch sum, which is the
    // same 117.
    expect(qty(GEBEDOL)).toBe(117);
    expect(batchRemaining(batch)).toBe(117);
  });

  it("a partial device receiving the last batch later does not inflate", async () => {
    // The case the ordering rule never covered: the device already had the
    // inventory row and an earlier batch, and the final batch event arrives on a
    // later sync. Used to land on 347 against a server truth of 247.
    insertInventory(rawDb(), { id: "inv-partial", drugId: AMOX_ID, quantity: 147 });
    insertBatch(rawDb(), {
      id: "b-partial-1",
      drugId: AMOX_ID,
      remaining: 147,
    });

    await applyEventLocally(batchEvent("b-partial-2", AMOX_ID, 100));
    expect(qty(AMOX_ID)).toBe(247);
  });
});

describe("two batches: Amoxicilin 147 + 100 = 247", () => {
  it("batches first, then the absolute branch_inventory event, yields 247", async () => {
    const b1 = "55555555-5555-5555-5555-555555555551";
    const b2 = "55555555-5555-5555-5555-555555555502";
    const inv = "66666666-6666-6666-6666-666666666666";

    await applyEventLocally(batchEvent(b1, AMOX_ID, 147));
    await applyEventLocally(batchEvent(b2, AMOX_ID, 100));
    // Absorbs the additive total.
    await applyEventLocally(biEvent(inv, AMOX_ID, 247));

    expect(qty(AMOX_ID)).toBe(247);
    expect(batchRemaining(b1)).toBe(147);
    expect(batchRemaining(b2)).toBe(100);
  });

  it("the previously-overshooting order (inventory first) now yields 247", async () => {
    const b1 = "77777777-7777-7777-7777-777777777771";
    const b2 = "77777777-7777-7777-7777-777777777702";
    const inv = "88888888-8888-8888-8888-888888888888";

    await applyEventLocally(biEvent(inv, AMOX_ID, 247));
    await applyEventLocally(batchEvent(b1, AMOX_ID, 147));
    await applyEventLocally(batchEvent(b2, AMOX_ID, 100));

    // Previously 494: 247 assigned, then 147 and 100 added on top.
    expect(qty(AMOX_ID)).toBe(247);
  });
});

describe("batch update after a consistent state", () => {
  it("a delta update lands on the server's new quantity", async () => {
    const batch = "99999999-9999-9999-9999-999999999999";
    const inv = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

    // Establish 117 consistently, batch first.
    await applyEventLocally(batchEvent(batch, GEBEDOL, 117));
    await applyEventLocally(biEvent(inv, GEBEDOL, 117));
    expect(qty(GEBEDOL)).toBe(117);

    // Server consumes 17: remaining 100, branch_inventory 100.
    seqCounter += 1;
    const update: EventEnvelope = {
      ...batchEvent(batch, GEBEDOL, 100, 117),
      event_id: `evt-batch-upd-${seqCounter}`,
      event_type: "drug_batch_updated",
    };
    await applyEventLocally(update);

    expect(batchRemaining(batch)).toBe(100);
    expect(qty(GEBEDOL)).toBe(100);
  });

  it("a trailing absolute branch_inventory event reconciles after a delta", async () => {
    const batch = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const inv = "cccccccc-cccc-cccc-cccc-cccccccccccc";
    await applyEventLocally(batchEvent(batch, GEBEDOL, 123));
    await applyEventLocally(biEvent(inv, GEBEDOL, 123));
    expect(qty(GEBEDOL)).toBe(123);

    seqCounter += 1;
    await applyEventLocally({
      ...batchEvent(batch, GEBEDOL, 117, 123),
      event_id: `evt-batch-upd2-${seqCounter}`,
      event_type: "drug_batch_updated",
    });
    // Device delta: 117 - 123 = -6 -> 117.
    expect(qty(GEBEDOL)).toBe(117);

    // Even if a projection had been missed, the absolute event repairs it.
    await applyEventLocally(biEvent("dddddddd-dddd-dddd-dddd-dddddddddddd", GEBEDOL, 117));
    expect(qty(GEBEDOL)).toBe(117);
  });
});

describe("add_drug_to_branch with quantity 0", () => {
  it("creates the row at 0 and does not invent stock", async () => {
    const inv = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee";
    await applyEventLocally(biEvent(inv, GEBEDOL, 0));
    expect(qty(GEBEDOL)).toBe(0);
    expect(qty(GEBEDOL, OTHER_BRANCH)).toBe(-1); // absent at the other branch
  });
});

describe("sellable_quantity is derived, not carried", () => {
  it("follows the batch rows the device received", async () => {
    const b1 = "12121212-1212-1212-1212-121212121211";
    const b2 = "12121212-1212-1212-1212-121212121202";
    const inv = "13131313-1313-1313-1313-131313131313";

    await applyEventLocally(batchEvent(b1, GEBEDOL, 117));
    await applyEventLocally(batchEvent(b2, GEBEDOL, 100));
    await applyEventLocally(biEvent(inv, GEBEDOL, 217));

    const sellable = rawDb()
      .prepare("SELECT sellable_quantity FROM branch_inventory WHERE drug_id = ?")
      .get(GEBEDOL) as { sellable_quantity: number };
    // Sum of unexpired batches with no leases.
    expect(sellable.sellable_quantity).toBe(217);
  });
});