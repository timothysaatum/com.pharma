import { describe, expect, it, vi } from "vitest";

import { ensureSuppressedCrrChangesSchema, migrate_v20, type Database } from "@/lib/localDb";

describe("local DB migration v20", () => {
  it("tracks sale projection changes that must not be CRR-pushed", async () => {
    const execute = vi.fn(async () => ({ rowsAffected: 1 }));
    const db = { execute } as unknown as Database;

    await migrate_v20(db);

    expect(execute.mock.calls.some(([sql]) =>
      String(sql).includes("crr_start_db_version"),
    )).toBe(true);
    expect(execute.mock.calls.some(([sql]) =>
      String(sql).includes("CREATE TABLE IF NOT EXISTS suppressed_crr_changes"),
    )).toBe(true);
    expect(execute).toHaveBeenLastCalledWith("PRAGMA user_version = 20");
  });

  it("can repair the suppression table independently of user_version", async () => {
    const execute = vi.fn(async () => ({ rowsAffected: 1 }));
    const db = { execute } as unknown as Database;

    await ensureSuppressedCrrChangesSchema(db);

    expect(execute.mock.calls.some(([sql]) =>
      String(sql).includes("CREATE TABLE IF NOT EXISTS suppressed_crr_changes"),
    )).toBe(true);
    expect(execute.mock.calls.some(([sql]) =>
      String(sql).includes("idx_suppressed_crr_version"),
    )).toBe(true);
    expect(execute.mock.calls.some(([sql]) =>
      String(sql).includes("PRAGMA user_version"),
    )).toBe(false);
  });
});
