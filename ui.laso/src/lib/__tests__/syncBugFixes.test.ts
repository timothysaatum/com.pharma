/**
 * syncBugFixes.test.ts
 * Tests for three critical sync bugs fixed in syncEngine.ts:
 *
 *   Bug 1a: server_wins conflict now passes conflictServerId to markSynced so the
 *           local row is renamed to the server UUID, preventing duplicate customers.
 *
 *   Bug 1b: markSynced for sales now updates offline_sales.id when the server
 *           assigned a different UUID, preventing duplicate pushes on next reconcile.
 *
 *   Bug 3:  reconcileOfflineSales only re-enqueues a sale when its sync_queue
 *           envelope is genuinely missing. It must NOT auto-reset a
 *           dead-lettered entry (attempts >= MAX_PUSH_ATTEMPTS) — doing so
 *           silently retried a permanently-failing sale forever, every sync
 *           cycle, because the reset ran before pendingFailures could ever
 *           observe it past the dead-letter threshold (finding S3 in
 *           docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md).
 *           Dead-lettered sales now stay visible until the user explicitly
 *           retries or voids them.
 *
 * Strategy: syncEngine.ts exports a singleton `syncEngine`. Private methods are
 * accessed via (syncEngine as any). DB and API calls are mocked at module level.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// ── Shared mock state ─────────────────────────────────────────────────────────

const dbExecuteMock = vi.fn().mockResolvedValue(undefined);
const dbSelectMock = vi.fn().mockResolvedValue([]);

const enqueueMock = vi.fn().mockResolvedValue(undefined);
const dequeueMock = vi.fn().mockResolvedValue(undefined);
const markQueueConflictMock = vi.fn().mockResolvedValue(undefined);
const markQueueErrorMock = vi.fn().mockResolvedValue(1);
const getPendingQueueMock = vi.fn().mockResolvedValue([]);
const getLastSyncAtMock = vi.fn().mockResolvedValue(null);
const setLastSyncAtMock = vi.fn().mockResolvedValue(undefined);
const getPendingConflictsMock = vi.fn().mockResolvedValue([]);
const getPendingFailuresMock = vi.fn().mockResolvedValue([]);
const getPermanentlyRejectedCrrRowsMock = vi.fn().mockResolvedValue([]);
const getPendingCountMock = vi.fn().mockResolvedValue(0);
const isCrrTableMock = vi.fn().mockResolvedValue(false);
const getCrrSiteIdMock = vi.fn().mockResolvedValue("site-local");
const getPendingSalesMock = vi.fn().mockResolvedValue([]);

vi.mock("@/lib/localDb", () => ({
  getDb: vi.fn(async () => ({
    select: dbSelectMock,
    execute: dbExecuteMock,
  })),
  enqueue: enqueueMock,
  dequeue: dequeueMock,
  markQueueConflict: markQueueConflictMock,
  markQueueError: markQueueErrorMock,
  getPendingQueue: getPendingQueueMock,
  getLastSyncAt: getLastSyncAtMock,
  setLastSyncAt: setLastSyncAtMock,
  getPendingConflicts: getPendingConflictsMock,
  getPendingFailures: getPendingFailuresMock,
  getPermanentlyRejectedCrrRows: getPermanentlyRejectedCrrRowsMock,
  suppressPermanentlyRejectedCrrRow: vi.fn().mockResolvedValue(undefined),
  getPendingCount: getPendingCountMock,
  isCrrTable: isCrrTableMock,
  getCrrSiteId: getCrrSiteIdMock,
  getCrrPushChanges: vi.fn().mockResolvedValue([]),
  applyCrrPullChanges: vi.fn().mockResolvedValue(undefined),
  applyCustomerMergeDirectives: vi.fn().mockResolvedValue(undefined),
  getCompatibleLocalColumns: vi.fn().mockResolvedValue(["id", "sync_status"]),
  notifySyncQueueChanged: vi.fn(),
  getPendingCrrRenumberAudits: vi.fn().mockResolvedValue([]),
  ensureSuppressedCrrChangesSchema: vi.fn().mockResolvedValue(undefined),
  getCrrPushChangesFromDb: vi.fn().mockResolvedValue([]),
  filterQueueByScope: vi.fn((rows: unknown[]) => rows),
}));

vi.mock("@/api/sync", () => ({
  syncApi: {
    push: vi.fn(),
    pull: vi.fn(),
    crrPush: vi.fn(),
    crrPull: vi.fn(),
  },
}));

vi.mock("@/lib/offlineSalesManager", () => ({
  offlineSalesManager: {
    getPendingSales: getPendingSalesMock,
  },
}));

vi.mock("@/lib/syncRetryBackoff", () => ({
  RetryBackoff: class {
    getDelay() { return 0; }
  },
}));

vi.mock("@/lib/withTimeout", () => ({
  promiseWithTimeout: (p: Promise<unknown>) => p,
}));

// ── Constants ─────────────────────────────────────────────────────────────────

const LOCAL_CUSTOMER_ID = "00000000-aaaa-aaaa-aaaa-000000000001";
const SERVER_CUSTOMER_ID = "99999999-bbbb-bbbb-bbbb-000000000002";
const LOCAL_SALE_ID    = "11111111-cccc-cccc-cccc-000000000003";
const SERVER_SALE_ID   = "88888888-dddd-dddd-dddd-000000000004";
const BRANCH_ID        = "branch-uuid-0001";
const MAX_PUSH_ATTEMPTS = 10;

// ── Bug 1a: server_wins passes serverId ──────────────────────────────────────

describe("Bug 1a: server_wins conflict — markSynced receives server UUID", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbSelectMock.mockResolvedValue([]);
    dbExecuteMock.mockResolvedValue(undefined);
    getPendingQueueMock.mockResolvedValue([
      {
        table_name: "customers",
        record_id: LOCAL_CUSTOMER_ID,
        operation: "create",
        sync_version: 1,
        payload_json: "{}",
        attempts: 0,
      },
    ]);
    getPendingSalesMock.mockResolvedValue([]);
  });

  it("renames local row to server UUID after server_wins conflict", async () => {
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");

    (syncEngine as any).branchId = BRANCH_ID;

    (syncApi.push as ReturnType<typeof vi.fn>).mockResolvedValue({
      accepted: [],
      conflicts: [
        {
          table_name: "customers",
          local_id: LOCAL_CUSTOMER_ID,
          resolution: "server_wins",
          server_record: {
            id: SERVER_CUSTOMER_ID,
            first_name: "Tim",
            last_name: "Saatum",
            sync_status: "synced",
            sync_version: 2,
          },
        },
      ],
      failed: [],
      total_accepted: 0,
      total_conflicts: 1,
      total_failed: 0,
      next_pull_timestamp: "2026-07-21T23:00:00Z",
    });

    await (syncEngine as any).push();

    expect(dequeueMock).toHaveBeenCalledWith("customers", LOCAL_CUSTOMER_ID);

    // markSynced must issue an UPDATE that renames id from LOCAL to SERVER
    const renameCall = dbExecuteMock.mock.calls.find(
      ([sql, params]: any[]) =>
        typeof sql === "string" &&
        sql.includes("UPDATE customers SET id") &&
        Array.isArray(params) &&
        params.includes(SERVER_CUSTOMER_ID) &&
        params.includes(LOCAL_CUSTOMER_ID)
    );
    expect(renameCall).toBeDefined();
  });
});

// ── Bug 1b: offline_sales.id updated on server UUID rename ───────────────────

describe("Bug 1b: markSynced — offline_sales.id updated when server assigns new UUID", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbExecuteMock.mockResolvedValue(undefined);
    // serverRows empty → rename path in markSynced
    dbSelectMock.mockResolvedValue([]);
  });

  it("sets offline_sales.id = serverId when server renamed the sale", async () => {
    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    await (syncEngine as any).markSynced("sales", LOCAL_SALE_ID, SERVER_SALE_ID);

    // Must have an UPDATE offline_sales SET id = $1 WHERE id = $3
    const idRenameCall = dbExecuteMock.mock.calls.find(
      ([sql, params]: any[]) =>
        typeof sql === "string" &&
        sql.includes("offline_sales") &&
        sql.includes("id = $1") &&
        Array.isArray(params) &&
        params.includes(SERVER_SALE_ID) &&
        params.includes(LOCAL_SALE_ID)
    );
    expect(idRenameCall).toBeDefined();
  });

  it("marks offline_sales synced with localId when IDs match", async () => {
    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    await (syncEngine as any).markSynced("sales", LOCAL_SALE_ID, LOCAL_SALE_ID);

    const syncedCall = dbExecuteMock.mock.calls.find(
      ([sql, params]: any[]) =>
        typeof sql === "string" &&
        sql.includes("offline_sales") &&
        sql.includes("sync_status = 'synced'") &&
        Array.isArray(params) &&
        params.includes(LOCAL_SALE_ID)
    );
    expect(syncedCall).toBeDefined();
  });
});

// ── Bug 3: reconcileOfflineSales resets dead-lettered entries ─────────────────

describe("Bug 3: reconcileOfflineSales — dead-lettered sales are left alone", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbExecuteMock.mockResolvedValue(undefined);
  });

  it("skips healthy queue entry (attempts < MAX_PUSH_ATTEMPTS)", async () => {
    getPendingSalesMock.mockResolvedValue([
      {
        id: LOCAL_SALE_ID,
        sync_status: "pending",
        sale_data: { id: LOCAL_SALE_ID, branch_id: BRANCH_ID },
        sale_items: [],
      },
    ]);
    dbSelectMock.mockResolvedValue([{ record_id: LOCAL_SALE_ID, attempts: 3 }]);

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    await (syncEngine as any).reconcileOfflineSales();

    expect(enqueueMock).not.toHaveBeenCalled();
    const resetCall = dbExecuteMock.mock.calls.find(
      ([sql]: any[]) =>
        typeof sql === "string" &&
        sql.includes("sync_queue") &&
        sql.includes("attempts = 0")
    );
    expect(resetCall).toBeUndefined();
  });

  it("does NOT reset a dead-lettered entry (attempts >= MAX_PUSH_ATTEMPTS) — it must stay blocked until the user retries or voids it", async () => {
    getPendingSalesMock.mockResolvedValue([
      {
        id: LOCAL_SALE_ID,
        sync_status: "pending",
        sale_data: { id: LOCAL_SALE_ID, branch_id: BRANCH_ID },
        sale_items: [],
      },
    ]);
    dbSelectMock.mockResolvedValue([{ record_id: LOCAL_SALE_ID, attempts: MAX_PUSH_ATTEMPTS }]);

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    await (syncEngine as any).reconcileOfflineSales();

    expect(enqueueMock).not.toHaveBeenCalled();
    const resetCall = dbExecuteMock.mock.calls.find(
      ([sql]: any[]) =>
        typeof sql === "string" &&
        sql.includes("sync_queue") &&
        sql.includes("attempts = 0")
    );
    expect(resetCall).toBeUndefined();
  });

  it("enqueues a sale with no queue entry", async () => {
    getPendingSalesMock.mockResolvedValue([
      {
        id: LOCAL_SALE_ID,
        sync_status: "pending",
        sale_data: { id: LOCAL_SALE_ID, branch_id: BRANCH_ID },
        sale_items: [],
      },
    ]);
    dbSelectMock.mockResolvedValue([]);

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    await (syncEngine as any).reconcileOfflineSales();

    expect(enqueueMock).toHaveBeenCalledWith(
      "sales",
      LOCAL_SALE_ID,
      "create",
      1,
      expect.objectContaining({ id: LOCAL_SALE_ID }),
      LOCAL_SALE_ID
    );
  });
});

// ── S4: error_code classification replaces prose string-matching ─────────────
// Regression coverage for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
// finding S4 — deferred-dependency retries must be driven by the typed
// PushResult.error_code, not by matching substrings out of free-text error
// prose (which was untyped, unversioned, and the "Prescription not yet
// synced" variant it checked for was never actually emitted by the backend).

describe("S4: deferred-dependency retries use error_code, not prose matching", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbSelectMock.mockResolvedValue([]);
    dbExecuteMock.mockResolvedValue(undefined);
    getPendingSalesMock.mockResolvedValue([]);
    getPendingQueueMock.mockResolvedValue([
      {
        table_name: "sales",
        record_id: LOCAL_SALE_ID,
        operation: "create",
        sync_version: 1,
        payload_json: "{}",
        attempts: 0,
      },
    ]);
  });

  it("skips the attempt-count increment when error_code is dependency_not_synced", async () => {
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    (syncApi.push as ReturnType<typeof vi.fn>).mockResolvedValue({
      accepted: [],
      conflicts: [],
      failed: [
        {
          table_name: "sales",
          local_id: LOCAL_SALE_ID,
          error: "Sale contains prescription-required drugs but the prescription is not yet synced.",
          error_code: "dependency_not_synced",
        },
      ],
      total_accepted: 0,
      total_conflicts: 0,
      total_failed: 1,
      next_pull_timestamp: "2026-07-21T23:00:00Z",
    });

    await (syncEngine as any).push();

    expect(markQueueErrorMock).toHaveBeenCalledWith(
      "sales",
      LOCAL_SALE_ID,
      expect.any(String),
      true
    );
  });

  it("does NOT skip the attempt-count increment for prose that merely resembles the deferred message but carries no error_code", async () => {
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    (syncApi.push as ReturnType<typeof vi.fn>).mockResolvedValue({
      accepted: [],
      conflicts: [],
      failed: [
        {
          table_name: "sales",
          local_id: LOCAL_SALE_ID,
          error: "Validation error: some unrelated field is not yet synced with the catalog.",
          // No error_code — this must NOT be treated as a deferred retry.
        },
      ],
      total_accepted: 0,
      total_conflicts: 0,
      total_failed: 1,
      next_pull_timestamp: "2026-07-21T23:00:00Z",
    });

    await (syncEngine as any).push();

    expect(markQueueErrorMock).toHaveBeenCalledWith(
      "sales",
      LOCAL_SALE_ID,
      expect.any(String),
      false
    );
  });
});

// ── pullTableFull must thread last_sync_at across pagination rounds ──────────
// Regression coverage for docs/reviews/2026-08-11-offline-first-architecture-review.md
// finding #5: once the server pull endpoint was fixed to actually paginate
// (has_more=true + a resume sync_timestamp for large tables), this loop's
// old behavior — never sending last_sync_at on any iteration — would spin
// forever re-fetching the same first page instead of terminating.

describe("pullTableFull threads last_sync_at across paginated rounds", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbSelectMock.mockResolvedValue([]);
    dbExecuteMock.mockResolvedValue(undefined);
  });

  it("passes the previous response's sync_timestamp as last_sync_at on the next call, and terminates", async () => {
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = BRANCH_ID;

    const pullMock = syncApi.pull as ReturnType<typeof vi.fn>;
    pullMock
      .mockResolvedValueOnce({
        drugs: [], drug_categories: [], price_contracts: [],
        customers: [{ id: "c1" }], prescriptions: [], branch_inventory: [],
        drug_batches: [], sales: [], purchase_orders: [],
        sync_timestamp: "2026-01-01T00:00:01Z",
        has_more: true,
        total_records: 1,
      })
      .mockResolvedValueOnce({
        drugs: [], drug_categories: [], price_contracts: [],
        customers: [{ id: "c2" }], prescriptions: [], branch_inventory: [],
        drug_batches: [], sales: [], purchase_orders: [],
        sync_timestamp: "2026-01-01T00:00:02Z",
        has_more: false,
        total_records: 1,
      });

    await (syncEngine as any).pullTableFull("customers");

    expect(pullMock).toHaveBeenCalledTimes(2);
    expect(pullMock.mock.calls[0][0].last_sync_at).toBeUndefined();
    expect(pullMock.mock.calls[1][0].last_sync_at).toBe("2026-01-01T00:00:01Z");
    expect(setLastSyncAtMock).toHaveBeenNthCalledWith(
      1, "2026-01-01T00:00:01Z", "customers", BRANCH_ID
    );
    expect(setLastSyncAtMock).toHaveBeenNthCalledWith(
      2, "2026-01-01T00:00:02Z", "customers", BRANCH_ID
    );
  });
});

// ── pushCrr() must suppress PERMANENTLY_REJECTED rows, not just log them ─────
// Regression coverage for a real, confirmed production bug: prescriptions
// and purchase_orders got permanently stuck failing "Unable to resolve
// encoded primary key" forever, blocking the whole push cursor every
// cycle. The server now classifies this specific, non-retryable case as
// PERMANENTLY_REJECTED (error_code); pushCrr() must suppress exactly those
// rows from all future push batches (see suppressPermanentlyRejectedCrrRow)
// so they stop blocking every other row, while an ordinary failure with no
// such error_code must be left alone to retry normally.

describe("pushCrr() suppresses PERMANENTLY_REJECTED rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dbSelectMock.mockResolvedValue([]);
    dbExecuteMock.mockResolvedValue(undefined);
  });

  it("suppresses a row classified permanently_rejected but not an ordinary failure in the same batch", async () => {
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");
    const localDb = await import("@/lib/localDb");
    (syncEngine as any).branchId = BRANCH_ID;

    (localDb.getCrrPushChanges as ReturnType<typeof vi.fn>).mockResolvedValue([
      { table: "prescriptions", pk: "pk-a", cid: "status", val: "active", col_version: 1, db_version: 5, site_id: "site-1", cl: 1, seq: 0 },
      { table: "branch_inventory", pk: "pk-b", cid: "quantity", val: 3, col_version: 1, db_version: 6, site_id: "site-1", cl: 1, seq: 0 },
    ]);

    (syncApi.crrPush as ReturnType<typeof vi.fn>).mockResolvedValue({
      results: [
        {
          table: "prescriptions",
          row_id: "pk-a",
          success: false,
          error: "prescriptions row was previously rejected during sync and cannot be recovered automatically.",
          error_code: "permanently_rejected",
          pk_b64: "b64:cGstYQ==",
        },
        {
          table: "branch_inventory",
          row_id: "row-b",
          success: false,
          error: "Postgres CHECK(selling_price >= 0) violation",
          // No error_code — an ordinary, potentially-transient failure.
        },
      ],
      total_received: 2,
      total_accepted: 0,
      total_failed: 2,
      sync_timestamp: "2026-08-09T09:00:00Z",
      merged_row_ids: [],
      accepted_audit_event_ids: [],
      audit_errors: {},
    });

    await (syncEngine as any).pushCrr();

    expect(localDb.suppressPermanentlyRejectedCrrRow).toHaveBeenCalledTimes(1);
    expect(localDb.suppressPermanentlyRejectedCrrRow).toHaveBeenCalledWith(
      expect.anything(), "prescriptions", "b64:cGstYQ==",
    );
  });

  it("does not advance the push cursor when a permanently-rejected row is suppressed this same cycle", async () => {
    // Suppression only takes effect for the *next* getCrrPushChanges call
    // (the filter lives in that query) -- this cycle must still treat the
    // batch as having had failures, or the cursor would advance past
    // in-flight local changes to OTHER rows that never actually synced.
    const { syncApi } = await import("@/api/sync");
    const { syncEngine } = await import("@/lib/syncEngine");
    const localDb = await import("@/lib/localDb");
    (syncEngine as any).branchId = BRANCH_ID;

    (localDb.getCrrPushChanges as ReturnType<typeof vi.fn>).mockResolvedValue([
      { table: "prescriptions", pk: "pk-a", cid: "status", val: "active", col_version: 1, db_version: 5, site_id: "site-1", cl: 1, seq: 0 },
    ]);
    (syncApi.crrPush as ReturnType<typeof vi.fn>).mockResolvedValue({
      results: [
        {
          table: "prescriptions", row_id: "pk-a", success: false,
          error: "permanently rejected", error_code: "permanently_rejected",
          pk_b64: "b64:cGstYQ==",
        },
      ],
      total_received: 1, total_accepted: 0, total_failed: 1,
      sync_timestamp: "2026-08-09T09:00:00Z", merged_row_ids: [],
      accepted_audit_event_ids: [], audit_errors: {},
    });

    const result = await (syncEngine as any).pushCrr();

    expect(result.hadFailures).toBe(true);
    expect(dbExecuteMock).not.toHaveBeenCalledWith(
      expect.stringContaining("INSERT INTO sync_meta"),
      expect.anything(),
    );
  });
});
