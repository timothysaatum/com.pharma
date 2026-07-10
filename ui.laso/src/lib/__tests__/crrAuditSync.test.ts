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
});
