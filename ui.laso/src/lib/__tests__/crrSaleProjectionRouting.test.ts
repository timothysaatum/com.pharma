import { describe, expect, it, vi } from "vitest";

import { getCrrPushChangesFromDb } from "@/lib/localDb";

describe("offline sale CRR projection routing", () => {
  it("excludes sales and their local inventory or prescription projections", async () => {
    const select = vi.fn(async () => []);

    await getCrrPushChangesFromDb({ select }, "site-1", 41);

    const [sql, values] = select.mock.calls[0];
    expect(sql).toContain(`"table" <> 'sales'`);
    expect(sql).toContain("NOT EXISTS");
    expect(sql).toContain("suppressed_crr_changes");
    expect(sql).toContain("suppressed.db_version = crsql_changes.db_version");
    expect(values).toEqual(["site-1", 41]);
  });
});
