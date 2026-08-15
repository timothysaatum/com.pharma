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

describe("getSellableQuantity — un-synced local batch cache", () => {
  afterEach(() => vi.clearAllMocks());

  function stubDb(
    db: Awaited<ReturnType<typeof getDb>>,
    { inventoryQty, sellableQty }:
      { inventoryQty: number | null; sellableQty: number },
  ) {
    if (inventoryQty === null) {
      return vi.spyOn(db, "select").mockResolvedValueOnce([]);
    } else {
      return vi.spyOn(db, "select").mockResolvedValueOnce([{ quantity: inventoryQty, sellable_quantity: sellableQty }]);
    }
  }

  it("falls back to cached inventory when no batch rows have synced yet", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: 100, sellableQty: 0 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.noBatchData).toBe(false);
    expect(info.notStocked).toBe(false);
    expect(info.sellable).toBe(0);
  });

  it("still blocks the sale when batches exist but all are expired", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: 100, sellableQty: 0 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.noBatchData).toBe(false);
    expect(info.sellable).toBe(0);
  });

  it("reports notStocked when the branch carries no inventory row at all", async () => {
    const db = await getDb();
    stubDb(db, { inventoryQty: null, sellableQty: 0 });

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.notStocked).toBe(true);
    expect(info.sellable).toBe(0);
  });
});
