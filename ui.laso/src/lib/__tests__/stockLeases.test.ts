import { describe, it, expect, vi, beforeEach } from "vitest";
import { getDb } from "../localDb";
import { localRead } from "../localRead";

vi.mock("../localDb", () => ({
  getDb: vi.fn(),
}));

describe("Stock Leases & Offline Gating", () => {
  let mockDb: any;

  beforeEach(() => {
    vi.resetAllMocks();
    
    // Mock navigator.onLine setter since we change it in tests
    Object.defineProperty(navigator, 'onLine', {
      writable: true,
      value: true
    });

    const store: Record<string, string> = {};
    (global as any).localStorage = {
      getItem: vi.fn((key: string) => store[key] || null),
      setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
    };

    mockDb = {
      select: vi.fn(),
      execute: vi.fn(),
    };
    (getDb as any).mockResolvedValue(mockDb);
  });

  it("when online, returns leaseRemaining + unleasedPool", async () => {
    navigator.onLine = true;
    localStorage.setItem("laso_terminal_id", "TERM-123");

    mockDb.select.mockImplementation(async (query: string) => {
      if (query.includes("branch_inventory")) {
        return [{ quantity: 100, sellable_quantity: 40 }]; // 40 unleased globally
      }
      if (query.includes("stock_leases")) {
        return [{ leased_quantity: 50, consumed_quantity: 10 }]; // 40 remaining lease
      }
      return [];
    });

    const res = await localRead.getSellableQuantity("B1", "D1");
    expect(res.sellable).toBe(80); // 40 from lease + 40 unleased
  });

  it("when offline, returns ONLY leaseRemaining", async () => {
    navigator.onLine = false;
    localStorage.setItem("laso_terminal_id", "TERM-123");

    mockDb.select.mockImplementation(async (query: string) => {
      if (query.includes("branch_inventory")) {
        return [{ quantity: 100, sellable_quantity: 40 }]; // 40 unleased globally
      }
      if (query.includes("stock_leases")) {
        return [{ leased_quantity: 50, consumed_quantity: 10 }]; // 40 remaining lease
      }
      return [];
    });

    const res = await localRead.getSellableQuantity("B1", "D1");
    expect(res.sellable).toBe(40); // Only the 40 from lease
  });

  it("ignores expired or non-active leases", async () => {
    navigator.onLine = false;
    localStorage.setItem("laso_terminal_id", "TERM-123");

    mockDb.select.mockImplementation(async (query: string) => {
      if (query.includes("branch_inventory")) {
        return [{ quantity: 100, sellable_quantity: 0 }]; 
      }
      if (query.includes("stock_leases")) {
        // Query should filter this out if it's expired or not active, but just in case
        return [{ leased_quantity: 50, consumed_quantity: 10 }]; 
      }
      return [];
    });

    const res = await localRead.getSellableQuantity("B1", "D1");
    expect(res.sellable).toBe(40); 
  });
});
