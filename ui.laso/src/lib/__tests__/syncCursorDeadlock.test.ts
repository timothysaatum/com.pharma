/**
 * syncCursorDeadlock.test.ts
 *
 * Regression tests for the pull-loop deadlock.
 *
 * The old loop set a `hitFailure` flag on the first projector throw, broke out
 * of the page, and only persisted the cursor when no event had failed. One
 * poison event therefore pinned the cursor at a fixed seq forever: every later
 * event was starved, nothing was recorded, nothing was retried, and the device
 * still reported itself as a healthy idle client. The only visible symptom was
 * stock figures quietly disagreeing with the server, with no error anywhere.
 *
 * These drive the real SyncEngine against a real in-memory SQLite (wired in at
 * the Tauri invoke boundary, see realDb.ts) and a stubbed server, and assert the
 * four properties that were missing: later events apply, the cursor advances,
 * the failure is recorded, and it is bounded.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, resetTables, rawDb } from "@/lib/__tests__/realDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";

/** Events the fake server will hand back. */
let serverEvents: EventEnvelope[] = [];

vi.mock("@/api/sync", () => ({
  syncApi: {
    pullEvents: vi.fn(async (afterSeq: number) => {
      const events = serverEvents.filter((e) => (e.seq ?? 0) > afterSeq);
      const nextAfterSeq = events.length > 0 ? Math.max(...events.map((e) => e.seq ?? 0)) : afterSeq;
      return {
        server_clock: new Date().toISOString(),
        events,
        has_more: false,
        next_after_seq: nextAfterSeq,
      };
    }),
    pushEvents: vi.fn(async () => ({ accepted: [], rejected: [], duplicates: [] })),
  },
}));

vi.mock("@/api/conflicts", () => ({
  conflictsApi: { list: vi.fn(async () => ({ conflicts: [] })) },
}));

function envelope(
  seq: number,
  eventId: string,
  eventType: string,
  payload: Record<string, unknown>
): EventEnvelope {
  return {
    event_id: eventId,
    seq,
    org_id: "org-1",
    aggregate_id: `agg-${eventId}`,
    aggregate_type: eventType.split("_")[0],
    event_type: eventType,
    schema_version: 1,
    payload,
    dependencies: [],
    authored_at: "2026-01-01T00:00:00Z",
    branch_id: BRANCH,
    received_at: "2026-01-01T00:00:00Z",
  } as unknown as EventEnvelope;
}

/**
 * A poison event: a server envelope whose `payload` is null.
 *
 * Every projector starts with `const p = e.payload` and then reads a field off
 * it, so a null payload throws a TypeError inside the projector. That is a real
 * failure from real projector code, and it is deterministic regardless of what
 * the local schema happens to contain — unlike depending on a missing table or
 * a NOT NULL column, which quietly stops throwing as migrations add them. A
 * malformed envelope from the server is exactly the kind of poison event that
 * used to freeze a device permanently.
 */
function poisonEvent(seq: number): EventEnvelope {
  return {
    event_id: `poison-${seq}`,
    seq,
    org_id: "org-1",
    aggregate_id: `agg-poison-${seq}`,
    aggregate_type: "drug",
    event_type: "drug_created",
    schema_version: 1,
    payload: null,
    dependencies: [],
    authored_at: "2026-01-01T00:00:00Z",
    branch_id: BRANCH,
    received_at: "2026-01-01T00:00:00Z",
  } as unknown as EventEnvelope;
}

/** A drug_created event that applies cleanly against the real schema. */
function goodEvent(seq: number, eventId: string, name: string): EventEnvelope {
  return envelope(seq, eventId, "drug_created", {
    organization_id: "org-1",
    name,
    sku: `SKU-${seq}`,
    drug_type: "otc",
  });
}

function readCursor(): number {
  const row = rawDb()
    .prepare("SELECT value FROM sync_meta WHERE key = 'event_pull_seq'")
    .get() as { value: string } | undefined;
  return row ? Number(row.value) : 0;
}

function readFailures(): Array<{ event_id: string; attempts: number; status: string; seq: number }> {
  return rawDb()
    .prepare("SELECT event_id, attempts, status, seq FROM sync_event_failures ORDER BY seq")
    .all() as Array<{ event_id: string; attempts: number; status: string; seq: number }>;
}

function drugNames(): string[] {
  return (rawDb().prepare("SELECT name FROM drugs ORDER BY name").all() as Array<{ name: string }>).map(
    (d) => d.name
  );
}

beforeAll(async () => {
  await installRealDb();
});

afterAll(() => {
  // Nothing to close: installRealDb owns the handle and vitest tears the
  // worker down. Explicitly NOT calling close here keeps afterEach able to
  // truncate between tests.
});

beforeEach(() => {
  resetTables(["drugs", "sync_event_failures", "sync_meta", "event_outbox"]);
  serverEvents = [];

  vi.stubGlobal("navigator", { onLine: true });
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {} });
  vi.stubGlobal("CustomEvent", class {
    constructor(public type: string, public init?: { detail?: unknown }) {}
  });
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

/**
 * Arm the engine WITHOUT start().
 *
 * start() fires an un-awaited this.sync() when it believes the backend is
 * reachable, and arms a 30s interval. That background sync sets _isSyncing, so
 * a later explicit await sync() returns immediately having done nothing, and
 * every assertion races the real work. Driving the engine directly keeps each
 * sync awaited and deterministic, which is what these tests need: they assert
 * cursor movement, not scheduling.
 */
async function armEngine() {
  const { syncEngine } = await import("@/lib/syncEngine");
  syncEngine.stop();
  const engine = syncEngine as unknown as {
    branchId: string | null;
    organizationId: string | null;
    _isSyncing: boolean;
    _dbInitError: string | null;
  };
  engine.branchId = BRANCH;
  engine.organizationId = "org-1";
  engine._isSyncing = false;
  engine._dbInitError = null;
  return syncEngine;
}

describe("pull-loop cursor deadlock", () => {
  it("applies events after a poison event and advances the cursor past it", async () => {
    // The exact shape the old code got wrong: seq 3 throws, seq 4..6 are fine.
    serverEvents = [
      goodEvent(1, "e1", "Drug A"),
      goodEvent(2, "e2", "Drug B"),
      poisonEvent(3),
      goodEvent(4, "e4", "Drug C"),
      goodEvent(5, "e5", "Drug D"),
      goodEvent(6, "e6", "Drug E"),
    ];

    const engine = await armEngine();
    await engine.sync();

    // Events after the poison one must have applied.
    expect(drugNames()).toEqual(["Drug A", "Drug B", "Drug C", "Drug D", "Drug E"]);

    // The cursor must be past the failure, not stuck before it.
    expect(readCursor()).toBe(6);

    // Exactly one failure recorded, for the poison event only.
    const failures = readFailures();
    expect(failures).toHaveLength(1);
    expect(failures[0]).toMatchObject({ event_id: "poison-3", seq: 3, status: "pending" });

    engine.stop();
  });

  it("reports a stall instead of a healthy idle client", async () => {
    serverEvents = [goodEvent(1, "e1", "Drug A"), poisonEvent(2), goodEvent(3, "e3", "Drug C")];

    const engine = await armEngine();
    await engine.sync();

    expect(engine.syncHealth).toMatchObject({
      pulledSeq: 3,
      serverHeadSeq: 3,
      failedCount: 1,
      quarantinedCount: 0,
      stalled: true,
    });

    engine.stop();
  });

  it("quarantines a permanently failing event after MAX_PROJECTION_ATTEMPTS", async () => {
    serverEvents = [poisonEvent(1), goodEvent(2, "e2", "Drug B")];

    const engine = await armEngine();

    await engine.sync();
    expect(readFailures()[0]).toMatchObject({ status: "pending", attempts: 1 });

    // Subsequent cycles retry the recorded failure and bump attempts.
    await engine.sync();
    await engine.sync();

    const final = readFailures();
    expect(final).toHaveLength(1);
    expect(final[0].attempts).toBeGreaterThanOrEqual(3);
    expect(final[0].status).toBe("quarantined");

    // The good event still applied: the poison event never blocked it.
    expect(drugNames()).toEqual(["Drug B"]);
    expect(readCursor()).toBe(2);

    expect(engine.syncHealth).toMatchObject({ failedCount: 0, quarantinedCount: 1 });

    engine.stop();
  });

  it("stops retrying a quarantined event while still serving later ones", async () => {
    serverEvents = [poisonEvent(1), goodEvent(2, "e2", "Drug B"), goodEvent(3, "e3", "Drug C")];

    const engine = await armEngine();

    for (let i = 0; i < 5; i += 1) await engine.sync();

    const attemptsWhenQuarantined = readFailures()[0].attempts;

    // New events keep arriving and keep applying.
    serverEvents = [...serverEvents, goodEvent(4, "e4", "Drug D")];
    await engine.sync();

    // The quarantined row's attempt count is frozen.
    expect(readFailures()[0].attempts).toBe(attemptsWhenQuarantined);
    expect(drugNames()).toContain("Drug D");

    engine.stop();
  });

  it("clears the failure record once a retried event applies", async () => {
    serverEvents = [goodEvent(1, "e1", "Drug A")];

    const engine = await armEngine();

    const { getDb, recordEventProjectionFailure } = await import("@/lib/localDb");
    await recordEventProjectionFailure(
      await getDb(),
      { event_id: "e1", seq: 1, event_type: "drug_created", aggregate_id: "agg-e1", branch_id: BRANCH },
      new Error("transient")
    );
    expect(readFailures()).toHaveLength(1);

    await engine.sync();

    // Applied on retry, so the record is gone rather than lingering as history.
    expect(readFailures()).toHaveLength(0);
    engine.stop();
  });

  it("does not wedge when the failure table itself cannot be written", async () => {
    // A projector that throws AND a failure-record write that throws must still
    // leave the cursor moving. Otherwise the new code would reintroduce the
    // original deadlock through a different door.
    serverEvents = [poisonEvent(1), goodEvent(2, "e2", "Drug B")];

    const localDb = await import("@/lib/localDb");
    const spy = vi
      .spyOn(localDb, "recordEventProjectionFailure")
      .mockRejectedValue(new Error("failure table unavailable"));

    const engine = await armEngine();
    await engine.sync();

    expect(readCursor()).toBe(2);
    expect(drugNames()).toEqual(["Drug B"]);
    spy.mockRestore();

    engine.stop();
  });
});