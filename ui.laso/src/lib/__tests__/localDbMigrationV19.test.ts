import { describe, expect, it, vi } from "vitest";

import { migrate_v19, type Database } from "@/lib/localDb";

describe("local DB migration v19", () => {
  it("routes sales away from CRR row merging", async () => {
    const execute = vi.fn(async () => ({ rowsAffected: 1 }));
    const db = { execute } as unknown as Database;

    await migrate_v19(db);

    expect(execute).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining("INSERT INTO sync_meta"),
      ["crr_enabled_sales", "0"],
    );
    expect(execute).toHaveBeenNthCalledWith(2, "PRAGMA user_version = 19");
  });
});
