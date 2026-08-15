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

describe("getSellableQuantity — projected field", () => {
  afterEach(() => vi.clearAllMocks());

  it("reads sellable_quantity directly from branch_inventory", async () => {
    const db = await getDb();
    vi.spyOn(db, "select").mockResolvedValueOnce([{ quantity: 100, sellable_quantity: 40 }]);

    const info = await localRead.getSellableQuantity("branch-1", "drug-1");

    expect(info.sellable).toBe(40);
    expect(info.totalValidBatch).toBe(40);
    expect(info.notStocked).toBe(false);
    expect(info.noBatchData).toBe(false);
    expect(db.select).toHaveBeenCalledTimes(1);
  });
});
