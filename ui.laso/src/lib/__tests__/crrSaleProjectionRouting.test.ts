import { describe, expect, it, vi } from "vitest";

import { getCrrPushChangesFromDb } from "@/lib/localDb";

describe("offline sale CRR projection routing", () => {
  it("excludes sales and their local inventory or prescription projections", async () => {
    const execute = vi.fn(async () => ({ rowsAffected: 0 }));
    const select = vi.fn(async () => []);

    await getCrrPushChangesFromDb({ execute, select }, "site-1", 41);

    expect(execute.mock.calls[0][0]).toContain("CREATE TABLE IF NOT EXISTS suppressed_crr_changes");
    const [sql, values] = select.mock.calls[0];
    expect(sql).toContain(`"table" <> 'sales'`);
    expect(sql).toContain("NOT EXISTS");
    expect(sql).toContain("suppressed_crr_changes");
    expect(sql).toContain("suppressed.db_version = crsql_changes.db_version");
    expect(values).toEqual(["site-1", 41]);
  });
});
