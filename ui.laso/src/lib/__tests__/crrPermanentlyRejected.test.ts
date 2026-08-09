import { describe, expect, it, vi } from "vitest";

import { suppressPermanentlyRejectedCrrRow, type Database } from "@/lib/localDb";

// Regression coverage for a real, confirmed production bug: prescriptions
// and purchase_orders (keep_both_renumber — never legitimately delete a
// row) got permanently stuck after a validation rejection tombstoned their
// shadow copy with nothing to restore. The client kept resending the exact
// same unpushed crsql_changes forever, and the server correctly kept
// failing forever, blocking every other row in every future push batch.
// The fix: the server now classifies this specific case as
// PERMANENTLY_REJECTED (see syncErrorCodes.ts), and the client must stop
// resending that one row by suppressing it from all future
// getCrrPushChanges batches (see crrSaleProjectionRouting.test.ts for the
// read-side filter this write-side function feeds).

function mockDb(overrides: {
  idRows?: { id: string }[];
  versionRows?: { db_version: number }[];
} = {}): { db: Database; execute: ReturnType<typeof vi.fn>; select: ReturnType<typeof vi.fn> } {
  const idRows = overrides.idRows ?? [{ id: "rx-local-id" }];
  const versionRows = overrides.versionRows ?? [{ db_version: 5 }, { db_version: 6 }];

  const execute = vi.fn(async () => ({ rowsAffected: 1 }));
  const select = vi.fn(async (query: string) => {
    if (query.includes("crsql_pack_columns")) return idRows as unknown;
    if (query.includes("DISTINCT db_version")) return versionRows as unknown;
    return [] as unknown;
  });

  return {
    db: { execute, select } as unknown as Database,
    execute,
    select,
  };
}

describe("suppressPermanentlyRejectedCrrRow", () => {
  it("resolves the local record id via crsql_pack_columns and inserts one suppression row per local db_version", async () => {
    const { db, execute, select } = mockDb();

    const result = await suppressPermanentlyRejectedCrrRow(
      db, "prescriptions", "b64:cGstYnl0ZXM=",
    );

    expect(result).toEqual({
      table: "prescriptions",
      recordId: "rx-local-id",
      reason: "permanently_rejected",
    });

    // Resolves the id from the client's OWN local table -- the server
    // couldn't resolve it (that's the whole point of this failure mode),
    // but the client authored the row itself, so it's still readable.
    const idCall = select.mock.calls.find((call: unknown[]) => (call[0] as string).includes("crsql_pack_columns"));
    expect(idCall![0]).toContain("SELECT id FROM prescriptions");
    expect(idCall![1]).toEqual([{ __laso_blob_b64: "cGstYnl0ZXM=" }]);

    // One INSERT per distinct local db_version found for this (table, pk).
    const inserts = execute.mock.calls.filter((call: unknown[]) => (call[0] as string).includes("INSERT OR IGNORE INTO suppressed_crr_changes"));
    expect(inserts).toHaveLength(2);
    expect(inserts[0][1]).toEqual(["prescriptions", 5, "rx-local-id", "permanently_rejected", expect.any(String)]);
    expect(inserts[1][1]).toEqual(["prescriptions", 6, "rx-local-id", "permanently_rejected", expect.any(String)]);
  });

  it("falls back to the pk itself as the suppression key when the local row can't be resolved either", async () => {
    // Defensive-only path -- shouldn't happen in practice (the client
    // authored this row), but the suppression insert has a NOT NULL
    // record_id column and must not crash if it ever does.
    const { db } = mockDb({ idRows: [], versionRows: [{ db_version: 9 }] });

    const result = await suppressPermanentlyRejectedCrrRow(
      db, "purchase_orders", "b64:cGstYnl0ZXM=",
    );

    expect(result.recordId).toBeNull();
  });

  it("rejects a table name outside the known CRR allowlist instead of interpolating it into SQL", async () => {
    const { db } = mockDb();

    await expect(
      suppressPermanentlyRejectedCrrRow(db, "users; DROP TABLE users;--", "b64:AA=="),
    ).rejects.toThrow(/unknown CRR table/i);
  });
});
