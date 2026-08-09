import { afterEach, describe, expect, it, vi } from "vitest";

describe("CRR migration audit sync", () => {
  afterEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it("uploads pending audit events even when there are no CRR row changes", async () => {
    const event = {
      event_id: "prescriptions:rx-loser:RX-7:RX-7-C",
      table_name: "prescriptions" as const,
      winner_id: "rx-winner",
      loser_id: "rx-loser",
      business_key_col: "prescription_number",
      old_business_key: "RX-7",
      new_business_key: "RX-7-C",
      renumbered_at: "2026-01-02T03:04:05Z",
    };
    const crrPush = vi.fn().mockResolvedValue({
      results: [], total_received: 0, total_accepted: 0, total_failed: 0,
      sync_timestamp: "2026-01-02T03:05:00Z", merged_row_ids: [],
      accepted_audit_event_ids: [event.event_id], audit_errors: {},
    });
    const markUploaded = vi.fn();
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPush, push: noop, pull: noop, crrPull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({ select: async () => [], execute: noop }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges: empty, applyCrrPullChanges: noop, getCrrSiteId: vi.fn(),
      getPendingCrrRenumberAudits: vi.fn().mockResolvedValue([event]),
      markCrrRenumberAuditsUploaded: markUploaded,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pushCrr();

    expect(crrPush).toHaveBeenCalledWith({
      branch_id: "branch-1",
      changes: [],
      audit_events: [event],
    });
    expect(markUploaded).toHaveBeenCalledWith([event.event_id]);
  });

  it("applies customer merge directives even when the CRR change batch is empty", async () => {
    const directive = {
      directive_version: 9,
      event_id: "customers:winner:loser",
      survivor_id: "winner",
      loser_id: "loser",
      merged_at: "2026-07-10T12:00:00Z",
    };
    const applyDirectives = vi.fn().mockResolvedValue(undefined);
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const crrPull = vi.fn().mockResolvedValue({
      crr_changes: [], crr_max_db_version: 0,
      customer_merge_directives: [directive],
      customer_merge_max_version: 9,
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPull, crrPush: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({ select: async () => [], execute }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges: empty, applyCrrPullChanges: noop,
      applyCustomerMergeDirectives: applyDirectives,
      getCrrSiteId: vi.fn().mockResolvedValue("local-site"),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pullCrr();

    expect(applyDirectives).toHaveBeenCalledWith([directive]);
    expect(crrPull).toHaveBeenCalledWith(expect.objectContaining({
      branch_id: "branch-1",
      customer_merge_since_version: 0,
    }));
    expect(execute).toHaveBeenCalledWith(
      expect.stringContaining("sync_meta"),
      ["customer_merge_directive_version", "9"],
    );
  });

  it("drains paginated CRR pulls without advancing the local push cursor", async () => {
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const applyChanges = vi.fn().mockResolvedValue(undefined);
    const crrPull = vi.fn()
      .mockResolvedValueOnce({
        crr_changes: [{ table: "audit_logs", pk: "a", cid: "action", val: "one", col_version: 1, db_version: 500, site_id: "server", cl: 1, seq: 1 }],
        crr_max_db_version: 500,
        customer_merge_directives: [],
        customer_merge_max_version: 0,
        has_more: true,
      })
      .mockResolvedValueOnce({
        crr_changes: [{ table: "audit_logs", pk: "b", cid: "action", val: "two", col_version: 1, db_version: 900, site_id: "server", cl: 1, seq: 2 }],
        crr_max_db_version: 900,
        customer_merge_directives: [],
        customer_merge_max_version: 0,
        has_more: false,
      });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPull, crrPush: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({ select: async () => [], execute }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges: empty, applyCrrPullChanges: applyChanges,
      applyCustomerMergeDirectives: noop,
      getCrrSiteId: vi.fn().mockResolvedValue("local-site"),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pullCrr();

    expect(crrPull).toHaveBeenNthCalledWith(1, expect.objectContaining({
      crr_since_db_version: 0,
    }));
    expect(crrPull).toHaveBeenNthCalledWith(2, expect.objectContaining({
      crr_since_db_version: 500,
    }));
    expect(applyChanges).toHaveBeenCalledTimes(2);
    expect(execute).toHaveBeenCalledWith(
      expect.stringContaining("sync_meta"),
      ["crr_pull_db_version", "500"],
    );
    expect(execute).toHaveBeenCalledWith(
      expect.stringContaining("sync_meta"),
      ["crr_pull_db_version", "900"],
    );
    expect(execute).not.toHaveBeenCalledWith(
      expect.any(String),
      expect.arrayContaining(["crr_push_db_version"]),
    );
  });

  it("does not advance the CRR pull cursor when local apply fails", async () => {
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const applyChanges = vi.fn().mockRejectedValue(new Error("blob insert failed"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const crrPull = vi.fn().mockResolvedValue({
      crr_changes: [{ table: "customers", pk: "b64:AQID", cid: "first_name", val: "Cassie", col_version: 1, db_version: 500, site_id: "server", cl: 1, seq: 1 }],
      crr_max_db_version: 500,
      customer_merge_directives: [],
      customer_merge_max_version: 0,
      has_more: false,
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPull, crrPush: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({ select: async () => [], execute }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges: empty, applyCrrPullChanges: applyChanges,
      applyCustomerMergeDirectives: noop,
      getCrrSiteId: vi.fn().mockResolvedValue("local-site"),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";

    await expect((syncEngine as any).pullCrr()).rejects.toThrow("blob insert failed");
    expect(execute).not.toHaveBeenCalledWith(
      expect.any(String),
      ["crr_pull_db_version", "500"],
    );

    warn.mockRestore();
    error.mockRestore();
  });

  it("uses the independent CRR push cursor when selecting local changes", async () => {
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const getCrrPushChanges = vi.fn().mockResolvedValue([
      { table: "sales", pk: "sale-1", cid: "sale_number", val: "S-1", col_version: 1, db_version: 13, site_id: "local-site", cl: 1, seq: 1 },
    ]);
    const crrPush = vi.fn().mockResolvedValue({
      results: [{ table: "sales", row_id: "sale-1", success: true }],
      total_received: 1,
      total_accepted: 1,
      total_failed: 0,
      sync_timestamp: "2026-07-12T00:00:00Z",
      merged_row_ids: ["sale-1"],
      accepted_audit_event_ids: [],
      audit_errors: {},
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPush, crrPull: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({
        select: async () => [
          { key: "crr_push_db_version", value: "7" },
          { key: "crr_db_version", value: "999" },
        ],
        execute,
      }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges, applyCrrPullChanges: noop, getCrrSiteId: vi.fn(),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pushCrr();

    expect(getCrrPushChanges).toHaveBeenCalledWith(7);
    expect(execute).toHaveBeenCalledWith(
      expect.stringContaining("sync_meta"),
      ["crr_push_db_version", "13"],
    );
  });

  it("ignores the legacy shared cursor when no push-specific cursor exists yet", async () => {
    // Regression coverage for a real bug: on a device that ran the old
    // pre-split code, `crr_db_version` was last written by pullCrr() with a
    // SERVER-scoped db_version watermark (pull ran after push in the old
    // sync() order, so pull's value always won). Falling back to that value
    // here compares LOCAL crsql_changes.db_version against a server-scale
    // number, which is almost always larger — `db_version > since` is false
    // for every local row, forever, and push silently never fires again.
    // A missing push-specific cursor must mean "start at 0", not "inherit
    // whatever pull last wrote".
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const getCrrPushChanges = vi.fn().mockResolvedValue([
      { table: "customers", pk: "b64:AQID", cid: "first_name", val: "New", col_version: 1, db_version: 5, site_id: "local-site", cl: 1, seq: 1 },
    ]);
    const crrPush = vi.fn().mockResolvedValue({
      results: [{ table: "customers", row_id: "cust-1", success: true }],
      total_received: 1,
      total_accepted: 1,
      total_failed: 0,
      sync_timestamp: "2026-07-12T00:00:00Z",
      merged_row_ids: ["cust-1"],
      accepted_audit_event_ids: [],
      audit_errors: {},
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPush, crrPull: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({
        // No "crr_push_db_version" row — only the huge legacy value that a
        // pre-split pull left behind.
        select: async () => [
          { key: "crr_db_version", value: "999" },
        ],
        execute,
      }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges, applyCrrPullChanges: noop, getCrrSiteId: vi.fn(),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pushCrr();

    expect(getCrrPushChanges).toHaveBeenCalledWith(0);
  });

  it("marks pushed prescriptions and customers synced via crsql_pack_columns, not raw pk", async () => {
    // Regression coverage for a real bug: a prior version of this fixup
    // compared `id = String(pk)` directly. `pk` is cr-sqlite's
    // packed-columns encoding, transported as a "b64:..." string by the
    // Tauri IPC layer — never equal to any real row id — so that UPDATE
    // silently matched zero rows every time, and prescription/customer
    // sync_status stayed 'pending' forever despite the push succeeding.
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const getCrrPushChanges = vi.fn().mockResolvedValue([
      { table: "prescriptions", pk: "b64:AQsGcngtTkVX", cid: "status", val: "active", col_version: 1, db_version: 13, site_id: "local-site", cl: 1, seq: 1 },
      { table: "customers", pk: "b64:AQsEY3VzdA==", cid: "first_name", val: "New", col_version: 1, db_version: 14, site_id: "local-site", cl: 1, seq: 2 },
    ]);
    const crrPush = vi.fn().mockResolvedValue({
      results: [
        { table: "prescriptions", row_id: "rx-1", success: true },
        { table: "customers", row_id: "cust-1", success: true },
      ],
      total_received: 2,
      total_accepted: 2,
      total_failed: 0,
      sync_timestamp: "2026-07-12T00:00:00Z",
      merged_row_ids: ["rx-1", "cust-1"],
      accepted_audit_event_ids: [],
      audit_errors: {},
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPush, crrPull: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({
        select: async () => [{ key: "crr_push_db_version", value: "7" }],
        execute,
      }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges, applyCrrPullChanges: noop, getCrrSiteId: vi.fn(),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      CRR_TABLES: new Set(), SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    await (syncEngine as any).pushCrr();

    const rxCall = execute.mock.calls.find(
      ([sql]: any[]) => typeof sql === "string" && sql.includes("UPDATE prescriptions"),
    );
    expect(rxCall).toBeDefined();
    expect(rxCall![0]).toContain("crsql_pack_columns(prescriptions.id)");
    expect(rxCall![0]).not.toMatch(/WHERE id = \$/);
    expect(rxCall![1]).toEqual([expect.any(String), "prescriptions", 7, 14]);

    const custCall = execute.mock.calls.find(
      ([sql]: any[]) => typeof sql === "string" && sql.includes("UPDATE customers"),
    );
    expect(custCall).toBeDefined();
    expect(custCall![0]).toContain("crsql_pack_columns(customers.id)");
    expect(custCall![1]).toEqual([expect.any(String), "customers", 7, 14]);
  });

  it("does not advance the CRR push cursor when any row fails", async () => {
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 1 });
    const noop = vi.fn().mockResolvedValue(undefined);
    const empty = vi.fn().mockResolvedValue([]);
    const getCrrPushChanges = vi.fn().mockResolvedValue([
      { table: "sales", pk: "sale-1", cid: "sale_number", val: "S-1", col_version: 1, db_version: 13, site_id: "local-site", cl: 1, seq: 1 },
    ]);
    const crrPush = vi.fn().mockResolvedValue({
      results: [{ table: "sales", row_id: "sale-1", success: false, error: "validation failed" }],
      total_received: 1,
      total_accepted: 0,
      total_failed: 1,
      sync_timestamp: "2026-07-12T00:00:00Z",
      merged_row_ids: [],
      accepted_audit_event_ids: [],
      audit_errors: {},
    });

    vi.doMock("@/api/sync", () => ({
      syncApi: { crrPush, crrPull: noop, push: noop, pull: noop },
    }));
    vi.doMock("@/lib/localDb", () => ({
      getDb: async () => ({
        select: async () => [
          { key: "crr_push_db_version", value: "7" },
        ],
        execute,
      }),
      getLastSyncAt: vi.fn(), setLastSyncAt: noop,
      getPendingQueue: empty, getPendingConflicts: empty, getPendingFailures: empty,
      resetPendingFailures: noop, dequeue: noop, markQueueError: noop,
      markQueueConflict: noop, getPendingCount: vi.fn().mockResolvedValue(0),
      getNextRetryAt: vi.fn().mockResolvedValue(null), requeueConflictForLocalWin: noop,
      getCrrPushChanges, applyCrrPullChanges: noop, getCrrSiteId: vi.fn(),
      getPendingCrrRenumberAudits: empty, markCrrRenumberAuditsUploaded: noop,
      ensureSuppressedCrrChangesSchema: noop,
      isCrrTable: vi.fn().mockResolvedValue(true), enqueue: noop,
      SYNC_QUEUE_CHANGED_EVENT: "test:queue",
    }));
    vi.doMock("@/api/client", () => ({ isOfflineError: () => false }));

    const { syncEngine } = await import("@/lib/syncEngine");
    (syncEngine as any).branchId = "branch-1";
    const result = await (syncEngine as any).pushCrr();

    expect(result).toEqual({ hadFailures: true });
    expect(getCrrPushChanges).toHaveBeenCalledWith(7);
    expect(execute).not.toHaveBeenCalledWith(
      expect.any(String),
      ["crr_push_db_version", "13"],
    );
  });
});
