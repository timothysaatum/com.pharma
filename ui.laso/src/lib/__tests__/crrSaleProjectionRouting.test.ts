import { describe, expect, it, vi } from "vitest";

import { getCrrPushChangesFromDb, type Database } from "@/lib/localDb";

describe("offline sale CRR projection routing", () => {
  it("excludes sales and their local inventory or prescription projections", async () => {
    const execute: Database["execute"] = vi.fn(async () => ({ rowsAffected: 0 }));
    // vi.fn() doesn't preserve a wrapped callback's own generic parameter
    // (it collapses T to unknown), so the mock is built untyped and cast to
    // the real Database["select"] signature — same effect as MockDb.select
    // in localDb.ts, which sidesteps this by not going through vi.fn at all.
    const select = vi.fn(async (_query: string, _values?: unknown[]) => [] as unknown) as unknown as Database["select"];

    await getCrrPushChangesFromDb({ execute, select }, "site-1", 41);

    expect(vi.mocked(execute).mock.calls[0][0]).toContain("CREATE TABLE IF NOT EXISTS suppressed_crr_changes");
    const [sql, values] = vi.mocked(select).mock.calls[0];
    expect(sql).toContain(`"table" <> 'sales'`);
    expect(sql).toContain("NOT EXISTS");
    expect(sql).toContain("suppressed_crr_changes");
    expect(sql).toContain("suppressed.db_version = crsql_changes.db_version");
    expect(values).toEqual(["site-1", 41]);
  });
});
