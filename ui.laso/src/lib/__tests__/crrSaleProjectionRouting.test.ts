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

  it("orders by db_version before seq, not seq alone", async () => {
    // Regression coverage for a real bug report: a customer created offline,
    // then a prescription for that customer moments later, never synced.
    // cr-sqlite's `seq` resets to 0 at the start of every transaction — it
    // only orders rows WITHIN one commit. `ORDER BY seq` alone leaves rows
    // from different transactions that happen to share a `seq` value in an
    // undefined relative order, confirmed against the real vendored
    // extension: a customer (db_version=1) and an unrelated prescription
    // (db_version=2) both start their own column changes at seq=0. If the
    // dependent row (prescription) is sent to the server before the row it
    // references (its customer), the server's FK validation rejects it and
    // the push cursor never advances — the record is stuck forever, not
    // just delayed. `db_version` is the only column that reflects true
    // chronological order across transactions.
    const execute: Database["execute"] = vi.fn(async () => ({ rowsAffected: 0 }));
    const select = vi.fn(async (_query: string, _values?: unknown[]) => [] as unknown) as unknown as Database["select"];

    await getCrrPushChangesFromDb({ execute, select }, "site-1", 0);

    const [sql] = vi.mocked(select).mock.calls[0];
    expect(sql).toContain("ORDER BY db_version, seq");
    expect(sql).not.toMatch(/ORDER BY seq(?!,)/);
  });

  it("re-wraps a b64-transport site_id into a blob bind parameter, not a text one", async () => {
    // Regression coverage for a real bug: getCrrSiteId() reads
    // `crsql_site_id()`, a genuine BLOB column. The Tauri IPC read path
    // (db.rs::row_to_json) serializes any BLOB as a "b64:..." STRING, so
    // the siteId this function receives is that string, not raw bytes.
    // Passing it straight through as a query parameter binds it as
    // Value::Text on the Rust side (json_to_rusqlite only produces
    // Value::Blob for the `{"__laso_blob_b64": ...}` object shape) — a
    // TEXT parameter can never equal a real BLOB crsql_changes.site_id
    // value, so `WHERE site_id = ?` silently matched zero rows forever,
    // even with a correct db_version cursor. This is why push never fired
    // for ANY table, not just customers/prescriptions.
    const execute: Database["execute"] = vi.fn(async () => ({ rowsAffected: 0 }));
    const select = vi.fn(async (_query: string, _values?: unknown[]) => [] as unknown) as unknown as Database["select"];

    await getCrrPushChangesFromDb({ execute, select }, "b64:qORLyNegRtumTMCx4pB5Sg==", 0);

    const [, values] = vi.mocked(select).mock.calls[0];
    expect(values).toEqual([{ __laso_blob_b64: "qORLyNegRtumTMCx4pB5Sg==" }, 0]);
  });
});
