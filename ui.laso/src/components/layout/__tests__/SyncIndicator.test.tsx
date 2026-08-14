/** @vitest-environment jsdom */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useSyncStatus", () => ({
  useSyncStatus: () => ({
    status: "error",
    pendingCount: 2,
    lastSyncAt: null,
    conflicts: [],
    failures: [
      {
        id: 1,
        table_name: "sales",
        record_id: "sale-1",
        operation: "create",
        sync_version: 1,
        payload_json: "{}",
        created_offline_at: "2026-06-26T00:00:00.000Z",
        attempts: 1,
        last_attempt_at: "2026-06-26T00:01:00.000Z",
        error: "Insufficient stock for Paracetamol",
        conflict_json: null,
        is_blocked: false,
        local_data: {},
      },
    ],
    syncNow: vi.fn(),
    resolveConflict: vi.fn(),
  }),
}));

vi.mock("@/components/layout/SyncConflictModal", () => ({
  SyncConflictModal: () => null,
}));

import { SyncIndicator } from "../SyncIndicator";

describe("SyncIndicator", () => {
  it("shows failed queue records instead of plain pending", () => {
    render(<SyncIndicator />);

    expect(screen.getByText("1 failed")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByText("1 record failed to sync")).toBeTruthy();
    expect(screen.getByText("sales: Insufficient stock for Paracetamol")).toBeTruthy();
  });
});
