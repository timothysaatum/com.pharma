/**
 * Phase 2 client half — a stranded cursor must heal itself.
 *
 * The bug
 * -------
 * `getEventPullSeq()` read a single UNSCOPED `sync_meta` row, while
 * `event_log.seq` is per-organisation (UNIQUE (org_id, seq)) and the pull
 * endpoint filters on the org in the JWT alone. A device that had synced org A
 * carried A's high-water mark into org B, and sat permanently above B's head.
 *
 * Worse, the server could not tell it. `EventPullResponse.next_after_seq` is a
 * paging cursor: on an empty page it echoes the caller's own `after_seq`. So the
 * device asked for events past 225, got none, was told "your next seq is 225",
 * and compared `pulledSeq < serverHeadSeq` → 225 < 225 → "healthy". It received
 * zero events, forever, and the UI said everything was fine.
 *
 * The fix, tested here
 * --------------------
 *   1. The cursor is keyed per org.
 *   2. The server's TRUE head (`server_head_seq`) is used, not the echo.
 *   3. `cursor > server_head_seq` rewinds to 0 and replays the log.
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb, resetTables } from "./realDb";

/**
 * `syncEngine` reaches the network through the `@/api/sync` module singleton, so
 * that module is the seam. `pullEvents` is driven directly rather than through
 * `start()`, which would install a 30s timer and window listeners this test does
 * not want.
 */
const pullEvents = vi.fn();
const conflictList = vi.fn(async () => ({ conflicts: [] }));
vi.mock("@/api/sync", () => ({ syncApi: { pullEvents } }));
vi.mock("@/api/conflicts", () => ({ conflictsApi: { list: conflictList } }));

const ORG_A = "2d060ef8-a302-447c-91f4-b2fd30268341";
const ORG_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";

interface PullResponse {
  server_clock: string;
  events: unknown[];
  has_more: boolean;
  next_after_seq: number;
  server_head_seq?: number;
}

function makeResponse(over: Partial<PullResponse> = {}): PullResponse {
  return {
    server_clock: new Date().toISOString(),
    events: [],
    has_more: false,
    next_after_seq: 0,
    ...over,
  };
}

/**
 * A SyncEngine bound to one branch/org, without starting the timer.
 *
 * `@/lib/syncEngine` is imported DYNAMICALLY on purpose: it imports localDb at
 * module load, and localDb must not resolve its database until installRealDb()
 * has installed the invoke bridge (otherwise it binds to MockDb and every
 * write silently vanishes).
 */
async function newEngine(orgId: string = ORG_A) {
    const { SyncEngine } = await import("@/lib/syncEngine");
    const engine = new SyncEngine();
    (engine as unknown as { branchId: string }).branchId = BRANCH;
    (engine as unknown as { organizationId: string }).organizationId = orgId;
    return engine as unknown as {
        pullEvents(): Promise<void>;
        cursorReset: { from: number; to: number; reason: string } | null;
        syncHealth: { serverHeadSeq: number | null };
    };
}

describe("Phase 2 — per-org cursor and stranded-cursor self-heal", () => {
    // One real database for the file, rows cleared between tests. localDb caches
    // its resolved handle, so calling installRealDb() again would migrate a
    // fresh in-memory DB that the cached handle no longer points at.
    beforeAll(async () => {
        await installRealDb();
    });

    beforeEach(() => {
        resetTables(["sync_meta", "applied_events"]);
        pullEvents.mockReset();
        conflictList.mockClear();
    });

    it("keys the pull cursor per organisation", async () => {
        const { getEventPullSeq, setEventPullSeq } = await import("@/lib/localDb");

        await setEventPullSeq(225, ORG_A);
        await setEventPullSeq(7, ORG_B);

        expect(await getEventPullSeq(ORG_A)).toBe(225);
        expect(await getEventPullSeq(ORG_B)).toBe(7);
    });

    it("does not inherit an unscoped legacy cursor into a new org", async () => {
        // The pre-v34 world: one unscoped row holding another org's position.
        rawDb()
            .prepare("INSERT INTO sync_meta(key, value) VALUES ('event_pull_seq', '225')")
            .run();
        const { getEventPullSeq } = await import("@/lib/localDb");

        // Reading 225 here would reproduce the exact bug v34 fixes, so the
        // legacy row must be ignored and the device must replay from the start.
        expect(await getEventPullSeq(ORG_A)).toBe(0);
        // It is left in place, unread, rather than deleted.
        const legacy = rawDb()
            .prepare("SELECT value FROM sync_meta WHERE key = 'event_pull_seq'")
            .all();
        expect(legacy).toHaveLength(1);
    });

    it("rewinds to 0 and replays when the cursor sits above the server head", async () => {
        const { getEventPullSeq, setEventPullSeq } = await import("@/lib/localDb");

        await setEventPullSeq(225, ORG_A);

        const pulled: number[] = [];
        pullEvents.mockImplementation(async (afterSeq: number) => {
            pulled.push(afterSeq);
            // Head is 3. Any cursor above 3 is impossible for this org.
            return makeResponse({ next_after_seq: afterSeq, server_head_seq: 3 });
        });

        const engine = await newEngine();
        await engine.pullEvents();

        // First call asked from the stranded cursor; the engine then rewound
        // and re-pulled from 0 within the same cycle.
        expect(pulled[0]).toBe(225);
        expect(pulled).toContain(0);

        // The stored cursor is now real, not stranded.
        expect(await getEventPullSeq(ORG_A)).toBe(0);
        // And the repair is observable rather than silent.
        expect(engine.cursorReset).toEqual({
            from: 225,
            to: 0,
            reason: "cursor_ahead_of_head",
        });
        expect(pullEvents.mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    it("does NOT rewind when the cursor equals the head", async () => {
        const { setEventPullSeq } = await import("@/lib/localDb");

        await setEventPullSeq(3, ORG_A);

        const pulled: number[] = [];
        pullEvents.mockImplementation(async (afterSeq: number) => {
            pulled.push(afterSeq);
            return makeResponse({ next_after_seq: afterSeq, server_head_seq: 3 });
        });

        const engine = await newEngine();
        await engine.pullEvents();

        expect(pulled).toEqual([3]);
        expect(engine.cursorReset).toBeNull();
    });

    it("does not rewind when the cursor is behind the head (the normal case)", async () => {
        const { setEventPullSeq } = await import("@/lib/localDb");

        await setEventPullSeq(1, ORG_A);

        const pulled: number[] = [];
        pullEvents.mockImplementation(async (afterSeq: number) => {
            pulled.push(afterSeq);
            return makeResponse({ next_after_seq: afterSeq, server_head_seq: 3 });
        });

        const engine = await newEngine();
        await engine.pullEvents();

        expect(pulled).toEqual([1]);
        expect(engine.cursorReset).toBeNull();
    });

    it("falls back to next_after_seq when talking to an older server", async () => {
        const { setEventPullSeq } = await import("@/lib/localDb");

        await setEventPullSeq(9, ORG_A);

        // No server_head_seq: a pre-v34 backend. Must not crash, and must NOT
        // rewind (an unknown head is not evidence of a stranded cursor).
        pullEvents.mockImplementation(async (afterSeq: number) =>
            makeResponse({ next_after_seq: afterSeq })
        );

        const engine = await newEngine();
        await engine.pullEvents();

        expect(engine.cursorReset).toBeNull();
        // Falls back to the old echo so the UI still shows a head.
        expect(engine.syncHealth.serverHeadSeq).toBe(9);
    });
});