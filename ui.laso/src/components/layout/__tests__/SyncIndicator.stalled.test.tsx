/** @vitest-environment jsdom */
/**
 * SyncIndicator must show a stalled state when the device and the server
 * disagree about the head, in EITHER direction.
 *
 * The regression: the lag term was one-sided (head > cursor only). A cursor that
 * had drifted above the real head computed head - cursor = 0 and rendered as a
 * healthy, fully-synced chip, while the device was permanently unable to
 * advance: every pull requests seq > cursor and comes back empty, silently.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mockUseSyncStatus = vi.fn();

vi.mock("@/hooks/useSyncStatus", () => ({
  useSyncStatus: () => mockUseSyncStatus(),
}));

vi.mock("@/components/layout/SyncConflictModal", () => ({
  SyncConflictModal: () => null,
}));

import { SyncIndicator } from "../SyncIndicator";

function health(pulledSeq: number, serverHeadSeq: number | null) {
  return {
    pulledSeq,
    serverHeadSeq,
    failedCount: 0,
    quarantinedCount: 0,
    stalled: false,
  };
}

function baseStatus(overrides: Record<string, unknown> = {}) {
  return {
    status: "idle",
    pendingCount: 0,
    lastSyncAt: new Date().toISOString(),
    conflicts: [],
    failures: [],
    syncNow: vi.fn(),
    resolveConflict: vi.fn(),
    discardFailure: vi.fn(),
    voidFailedSale: vi.fn(),
    ...overrides,
  };
}

describe("SyncIndicator stalled states", () => {
  it("reports the exact position when the cursor is AHEAD of the head", () => {
    // The live device: event_pull_seq 225 against an org head of 69.
    mockUseSyncStatus.mockReturnValue(
      baseStatus({ health: health(225, 69) })
    );

    render(<SyncIndicator />);

    expect(screen.getByText("Sync stalled at 225 of 69")).toBeTruthy();
  });

  it("does not claim to be behind when the cursor is ahead", () => {
    mockUseSyncStatus.mockReturnValue(baseStatus({ health: health(225, 69) }));

    render(<SyncIndicator />);

    // head - cursor is -156; presenting that as "Behind by -156" was nonsense.
    expect(screen.queryByText(/Behind by/)).toBeNull();
  });

  it("shows the lag when the head is ahead of the cursor", () => {
    mockUseSyncStatus.mockReturnValue(baseStatus({ health: health(5, 69) }));

    render(<SyncIndicator />);

    expect(screen.getByText("Behind by 64")).toBeTruthy();
  });

  it("shows a normal timestamp when cursor equals head", () => {
    mockUseSyncStatus.mockReturnValue(baseStatus({ health: health(69, 69) }));

    render(<SyncIndicator />);

    expect(screen.getByText("Just now")).toBeTruthy();
    expect(screen.queryByText(/Sync stalled/)).toBeNull();
    expect(screen.queryByText(/Behind by/)).toBeNull();
  });

  it("does not claim a position while the head is unknown", () => {
    mockUseSyncStatus.mockReturnValue(baseStatus({ health: health(225, null) }));

    render(<SyncIndicator />);

    expect(screen.queryByText(/Sync stalled/)).toBeNull();
  });

  it("shows the stalled position in the collapsed tooltip", () => {
    mockUseSyncStatus.mockReturnValue(baseStatus({ health: health(225, 69) }));

    render(<SyncIndicator collapsed />);

    const button = screen.getByRole("button");
    expect(button.getAttribute("title")).toContain("Sync stalled at 225 of 69");
  });
});