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
    errorMessage: (err: any) => {
      if (err instanceof Error) return err.message;
      if (err && typeof err === "object" && "message" in err) return String(err.message);
      return String(err);
    },
  };
});

describe("localRead customer search parameter binding and error formatting", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("should query local database using explicit ? placeholders in the correct order", async () => {
    const mockDb = await getDb();
    const selectSpy = vi.spyOn(mockDb, "select").mockResolvedValue([]);

    await localRead.searchCustomerMatches("john", 10, "org-123");

    expect(selectSpy).toHaveBeenCalled();
    const [sqlQuery, sqlParams] = selectSpy.mock.calls[0];

    // Assert that the generated SQL uses explicit ? placeholders instead of $ named placeholders
    expect(sqlQuery).toContain("?1");
    expect(sqlQuery).toContain("?2");
    expect(sqlQuery).toContain("?3");
    expect(sqlQuery).not.toContain("$1");
    expect(sqlQuery).not.toContain("$2");
    expect(sqlQuery).not.toContain("$3");

    // Assert values mapping: term at index 0, limit at index 1, organization_id at index 2
    expect(sqlParams).toEqual(["%john%", 10, "org-123"]);
  });

  it("should handle plain object errors from database without stringifying to [object Object]", async () => {
    const mockDb = await getDb();
    // Simulate database query failure throwing a plain object (which is common across Tauri bridges)
    vi.spyOn(mockDb, "select").mockRejectedValueOnce({
      message: "database disk image is malformed",
      code: 11
    });

    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    // First search fails, it retries via simple query (which we let mock succeed)
    vi.spyOn(mockDb, "select").mockResolvedValueOnce([]);

    await localRead.searchCustomerMatches("john", 10, "org-123");

    // The warning message should parse the message string and not print "[object Object]"
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("[localRead] Customer search failed"),
      "database disk image is malformed"
    );

    warnSpy.mockRestore();
  });
});
