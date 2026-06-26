import { describe, expect, it } from "vitest";
import { isQueuedRecordInScope } from "@/lib/localDb";
import type { QueuedRecord } from "@/lib/localDb";

function queuedRecord(
  tableName: string,
  payload: Record<string, unknown> | string
): QueuedRecord {
  return {
    id: 1,
    table_name: tableName,
    record_id: "record-1",
    operation: "create",
    sync_version: 1,
    payload_json: typeof payload === "string" ? payload : JSON.stringify(payload),
    created_offline_at: "2026-06-26T00:00:00.000Z",
    attempts: 0,
    last_attempt_at: null,
    error: null,
    conflict_json: null,
  };
}

describe("local sync queue scoping", () => {
  it("keeps branch-owned queue records limited to the active branch", () => {
    const scope = { organizationId: "org-new", branchId: "branch-new" };

    expect(
      isQueuedRecordInScope(
        queuedRecord("sales", {
          organization_id: "org-old",
          branch_id: "branch-old",
        }),
        scope
      )
    ).toBe(false);

    expect(
      isQueuedRecordInScope(
        queuedRecord("sales", {
          organization_id: "org-new",
          branch_id: "branch-new",
        }),
        scope
      )
    ).toBe(true);
  });

  it("keeps organization-owned queue records limited to the active organization", () => {
    const scope = { organizationId: "org-new", branchId: "branch-new" };

    expect(
      isQueuedRecordInScope(
        queuedRecord("customers", { organization_id: "org-old" }),
        scope
      )
    ).toBe(false);

    expect(
      isQueuedRecordInScope(
        queuedRecord("customers", { organization_id: "org-new" }),
        scope
      )
    ).toBe(true);
  });

  it("does not include malformed scoped records", () => {
    expect(
      isQueuedRecordInScope(
        queuedRecord("sales", "{not-json"),
        { organizationId: "org-new", branchId: "branch-new" }
      )
    ).toBe(false);
  });
});
