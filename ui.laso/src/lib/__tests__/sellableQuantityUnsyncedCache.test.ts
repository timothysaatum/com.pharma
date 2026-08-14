import { afterEach, describe, expect, it, vi } from "vitest";
import { localRead } from "@/lib/localRead";
import { getDb } from "@/lib/localDb";

vi.mock("@/lib/localDb", () => {
  const mockDbSelect = vi.fn();
  const mockDbExecute = vi.fn();
  return {
    getDb: vi.fn().mockResolvedValue({
      select: mockDbSelect,
      execute: mockDbExecute,
    }),
    errorMessage: (err: unknown) =>
      err instanceof Error ? err.message : String(err),
  };
});

/**
 * Regression cover for the POS "stocked but unsellable" defect.
 *
 * `drug_batches` is only populated by a completed sync, while
 * `branch_inventory` is also written by the online inventory cache. A device
 * that has never finished a sync therefore has stock rows but zero batch rows.
 * The old expiry-only query read that as "every batch is expired" and blocked
 * the sale, so a branch holding 100 units showed "100 available" in the search
 * list and "No valid non-expired batches. Cannot sell." in the cart.
 */
describe("getSellableQuantity — un-synced local batch cache", () => {
  afterEach(() => vi.clearAllMocks());

  /** select() is called in order: branch_inventory, valid batches, batch count. */
  function stubDb(
    db: Awaited<ReturnType<typeof getDb>>,
    { inventoryQty, validBatchQty, batchRowCount }:
      { inventoryQty: number | null; validBatchQty: number; batchRowCount: number },
  ) {
    return vi.spyOn(db, "select")
      .mockResolvedValueOnce(inventoryQty === null ? [] : [{ quantity: inventoryQty }])
      .mockResolvedValueOnce([{ remaining: validBatchQty }])
      .mockResolvedValueOnce([{ n: batchRowCount }]);
  }

  it("falls back to cached inventory when no batch rows have synced yet", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: 100, validBatchQty: 0, batchRowCount: 0 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.noBatchData).toBe(true);
    expect(info.notStocked).toBe(false);
    // The cashier must not be blocked: the server already reduced this figure
    // to unexpired stock, and re-validates again at commit time.
    expect(info.sellable).toBe(100);
  });

  it("still blocks the sale when batches exist but all are expired", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: 100, validBatchQty: 0, batchRowCount: 3 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.noBatchData).toBe(false);
    expect(info.sellable).toBe(0);
  });

  it("prefers the real batch sum over cached inventory once batches exist", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: 100, validBatchQty: 40, batchRowCount: 2 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.noBatchData).toBe(false);
    expect(info.totalValidBatch).toBe(40);
    expect(info.sellable).toBe(40);
  });

  it("reports notStocked when the branch carries no inventory row at all", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: null, validBatchQty: 0, batchRowCount: 0 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.notStocked).toBe(true);
    expect(info.sellable).toBe(0);
  });
});
