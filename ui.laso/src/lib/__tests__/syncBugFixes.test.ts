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
 *   Bug 3:  reconcileOfflineSales now resets dead-lettered sync_queue entries
 *           (attempts >= MAX_PUSH_ATTEMPTS) so they can be retried.
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
      ([sql, params]: [string, string[]]) =>
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
      ([sql, params]: [string, string[]]) =>
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
      ([sql, params]: [string, string[]]) =>
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

describe("Bug 3: reconcileOfflineSales — dead-lettered sales are reset for retry", () => {
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
      ([sql]: [string]) =>
        typeof sql === "string" &&
        sql.includes("sync_queue") &&
        sql.includes("attempts = 0")
    );
    expect(resetCall).toBeUndefined();
  });

  it("resets dead-lettered entry (attempts >= MAX_PUSH_ATTEMPTS) without re-enqueuing", async () => {
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
      ([sql]: [string]) =>
        typeof sql === "string" &&
        sql.includes("sync_queue") &&
        sql.includes("attempts = 0") &&
        sql.includes("error = NULL")
    );
    expect(resetCall).toBeDefined();
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
