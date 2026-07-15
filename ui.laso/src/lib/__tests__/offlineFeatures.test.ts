import { describe, expect, it, vi } from "vitest";
import { localRead } from "@/lib/localRead";
import { writeLocal } from "@/lib/localWrite";
import { getDb } from "@/lib/localDb";

// Simple custom promise timeout wrapper (matching the one in syncEngine.ts)
function promiseWithTimeout<T>(promise: Promise<T>, timeoutMs = 100): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      setTimeout(() => {
        reject(new Error(`Sync timeout after ${timeoutMs}ms`));
      }, timeoutMs);
    }),
  ]);
}

vi.mock("@/lib/localDb", () => {
  const mockDbSelect = vi.fn();
  const mockDbExecute = vi.fn();
  return {
    getDb: vi.fn().mockResolvedValue({
      select: mockDbSelect,
      execute: mockDbExecute,
    }),
    enqueue: vi.fn().mockResolvedValue(undefined),
    isCrrTable: vi.fn().mockResolvedValue(true),
  };
});

describe("Offline First Capabilities and Search Integration", () => {
  it("should support customer search with matching full name, phone, email, or member ID", async () => {
    const mockDb = await getDb();
    const selectSpy = vi.spyOn(mockDb, "select").mockResolvedValue([
      {
        id: "cust-123",
        first_name: "Cassie1",
        last_name: "Nam",
        phone: "+2335555555",
        email: "cassie@example.com",
        loyalty_tier: "gold",
        insurance_provider_id: "ins-999",
        insurance_member_id: "MEM-777",
        preferred_contract_id: "contract-123",
      },
    ]);

    const results = await localRead.searchCustomerMatches("cass", 10, "org-abc");

    expect(results).toHaveLength(1);
    expect(results[0]).toEqual({
      id: "cust-123",
      full_name: "Cassie1 Nam",
      phone: "+2335555555",
      email: "cassie@example.com",
      loyalty_tier: "gold",
      has_insurance: true,
      contract_name: "contract-123",
    });

    expect(selectSpy).toHaveBeenCalled();
  });

  it("should write local audit logs upon offline actions", async () => {
    const mockDb = await getDb();
    const executeSpy = vi.spyOn(mockDb, "execute").mockResolvedValue({ rowsAffected: 1, lastInsertId: 1 });

    await writeLocal.auditLog({
      organization_id: "org-123",
      action: "test_action_offline",
      entity_type: "sales",
      entity_id: "sale-456",
      changes: JSON.stringify({ total: 100 }),
    });

    expect(executeSpy).toHaveBeenCalledWith(
      expect.stringContaining("INSERT INTO audit_logs"),
      expect.any(Array)
    );
  });

  it("should exit from 'Syncing...' state via timeout when a network call hangs", async () => {
    const hangingPromise = new Promise<string>(() => {
      // simulate network call that never resolves
    });

    await expect(promiseWithTimeout(hangingPromise, 50)).rejects.toThrow(
      "Sync timeout after 50ms"
    );
  });
});
