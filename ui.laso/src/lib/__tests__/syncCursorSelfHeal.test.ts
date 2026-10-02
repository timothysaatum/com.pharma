/**
 * Cursor self-healing and per-org scoping.
 *
 * The defect: the server reported its head by echoing after_seq back in
 * next_after_seq when a page came back empty. A device whose cursor had drifted
 * ABOVE the real org head therefore read its own cursor back as "the head",
 * computed lag as head - cursor = 0, and rendered a healthy, fully-synced chip
 * while being permanently unable to advance: every pull asks for seq > 225 when
 * the org only has 69 events, so it returns nothing, forever, silently.
 *
 * Drives the real SyncEngine through its public API against real in-memory
 * SQLite and a stubbed server that behaves the way the fixed server behaves: it
 * always reports the true per-org head, including on an empty page.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, resetTables, rawDb } from "@/lib/__tests__/realDb";
import type { AggregateType, EventEnvelope } from "@/lib/eventEnvelope";

const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const OTHER_ORG = "11111111-1111-1111-1111-111111111111";

/** Per-org event logs the fake server holds. */
let serverLog: Record<string, EventEnvelope[]> = {};
/** after_seq of every pull, to prove a reset re-pulled from the start. */
let pullCalls: number[] = [];
/** Head override, to simulate events landing after the page was computed. */
let headOverride: number | null = null;
/** Model an older server that does not send the field at all. */
let omitHead = false;
/** Which org the caller is authenticated as, as the real server sees it. */
let activeOrg = ORG;

function logFor(org: string): EventEnvelope[] {
  return serverLog[org] ?? [];
}

vi.mock("@/api/sync", () => ({
  syncApi: {
    pullEvents: vi.fn(async (afterSeq: number, limit = 200) => {
      pullCalls.push(afterSeq);
      // The server scopes a pull to the CALLER's org; it takes no org parameter.
      const all = logFor(activeOrg);
      const events = all
        .filter((e) => (e.seq ?? 0) > afterSeq)
        .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
      const page = events.slice(0, limit);
      const hasMore = events.length > limit;
      const nextAfterSeq = page.length > 0 ? (page[page.length - 1].seq ?? afterSeq) : afterSeq;
      const realHead = all.reduce((m, e) => Math.max(m, e.seq ?? 0), 0);
      return {
        server_clock: new Date().toISOString(),
        events: page,
        has_more: hasMore,
        next_after_seq: nextAfterSeq,
        // The fix: the true head, on EVERY page including an empty one.
        ...(omitHead ? {} : { server_head_seq: headOverride ?? realHead }),
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
  payload: Record<string, unknown>,
  orgId = ORG
): EventEnvelope {
  return {
    event_id: eventId,
    seq,
    org_id: orgId,
    aggregate_id: String(payload.drug_id ?? `agg-${eventId}`),
    aggregate_type: eventType.split("_")[0] as AggregateType,
    event_type: eventType,
    schema_version: 1,
    payload,
    dependencies: [],
    authored_at: "2026-09-19T18:01:58Z",
    authored_by: "bae475d9-994a-4d5b-abb2-32aa4b082602",
    branch_id: BRANCH,
    hash_self: "h",
    hash_prev: "p",
  };
}

/** The five catalogue drugs the real device ended up short of. */
const CATALOG = [
  "8d4cc1a7-03c7-4a6a-8080-2bda5def026f",
  "9038e0e0-459f-4a6f-8bd1-72bb2b8447e0",
  "c13dae4f-8f37-4558-a723-18c69bfd3ad0",
  "378b5603-324b-47cd-87ff-069e7eb5ff77",
  "5dadb522-766f-4789-930e-dddefb0964ef",
];

function catalogEvents(org = ORG, prefix = "evt"): EventEnvelope[] {
  return CATALOG.map((id, i) =>
    envelope(
      i + 1,
      `${prefix}-drug-${i + 1}`,
      "drug_created",
      {
        drug_id: id,
        organization_id: org,
        name: `Drug ${i}`,
        sku: null,
        unit_price: 5,
        unit_of_measure: "unit",
        reorder_level: 10,
        reorder_quantity: 0,
        is_active: true,
      },
      org
    )
  );
}

let syncEngine: typeof import("@/lib/syncEngine").syncEngine;

beforeAll(async () => {
  await installRealDb();
  ({ syncEngine } = await import("@/lib/syncEngine"));
});

beforeEach(() => {
  resetTables([
    "drugs",
    "branch_inventory",
    "drug_batches",
    "stock_leases",
    "sync_meta",
    "event_outbox",
    "applied_events",
    "sync_event_failures",
    "pending_conflicts",
  ]);
  serverLog = { [ORG]: [], [OTHER_ORG]: [] };
  pullCalls = [];
  headOverride = null;
  omitHead = false;
  activeOrg = ORG;
  // Node environment: there is no navigator, so stand one in.
  vi.stubGlobal("navigator", { onLine: true });
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
});

afterEach(() => {
  syncEngine.stop();
  vi.restoreAllMocks();
});

afterAll(() => {
  syncEngine.stop();
});

function setCursor(org: string, value: number) {
  rawDb()
    .prepare("INSERT INTO sync_meta(key, value) VALUES(?, ?)")
    .run(`event_pull_seq:${org}`, String(value));
}

function cursorFor(org: string): number {
  const row = rawDb()
    .prepare("SELECT value FROM sync_meta WHERE key = ?")
    .get(`event_pull_seq:${org}`) as { value: string } | undefined;
  return row ? Number(row.value) : 0;
}

function drugCount(): number {
  return (rawDb().prepare("SELECT COUNT(*) AS c FROM drugs").get() as { c: number }).c;
}

/**
 * Arm the engine WITHOUT start().
 *
 * start() fires an un-awaited this.sync() and arms a 30s interval, so a later
 * awaited sync() would race it and return having done nothing. Setting the two
 * ids directly keeps each sync deterministic, which is what these assertions
 * need: they are about cursor movement, not scheduling.
 */
async function pullAs(org: string) {
  syncEngine.stop();
  activeOrg = org;
  const engine = syncEngine as unknown as {
    branchId: string | null;
    organizationId: string | null;
    _isSyncing: boolean;
    _dbInitError: string | null;
  };
  engine.branchId = BRANCH;
  engine.organizationId = org;
  engine._isSyncing = false;
  engine._dbInitError = null;
  await syncEngine.sync();
}

describe("cursor ahead of the server head", () => {
  it("resets to 0, re-pulls from the start, and lands on the real head", async () => {
    serverLog[ORG] = catalogEvents();
    // Drift the cursor far above the org head, exactly as the live device was.
    setCursor(ORG, 225);
    expect(cursorFor(ORG)).toBe(225);

    await pullAs(ORG);

    // The five catalogue drugs are now present locally.
    expect(drugCount()).toBe(5);
    expect(cursorFor(ORG)).toBe(5);
    // The reset really did re-pull from the start.
    expect(pullCalls[0]).toBe(225);
    expect(pullCalls).toContain(0);
  });

  it("warns on reset so the drift is visible in the console", async () => {
    serverLog[ORG] = catalogEvents();
    setCursor(ORG, 225);

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await pullAs(ORG);

    const drift = warn.mock.calls.find((c) =>
      String(c[0]).includes("ahead of the server head")
    );
    expect(drift, "expected a cursor-drift warning").toBeTruthy();
    const text = String(drift?.[0]);
    expect(text).toContain("225");
    expect(text).toContain("5");
  });

  it("leaves a healthy cursor alone and does not warn", async () => {
    serverLog[ORG] = catalogEvents();
    setCursor(ORG, 2);

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await pullAs(ORG);

    expect(cursorFor(ORG)).toBe(5);
    expect(drugCount()).toBe(3);
    expect(
      warn.mock.calls.find((c) => String(c[0]).includes("ahead of the server head"))
    ).toBeUndefined();
  });

  it("tolerates a server that omits server_head_seq", async () => {
    // Older server shape: no server_head_seq at all. Must not crash or reset.
    serverLog[ORG] = catalogEvents();
    setCursor(ORG, 2);
    omitHead = true;

    await expect(pullAs(ORG)).resolves.not.toThrow();
    // Falls back to next_after_seq rather than inventing a head.
    expect(cursorFor(ORG)).toBe(5);
  });
});

describe("per-org cursor scoping", () => {
  it("keeps a separate cursor per organization", async () => {
    serverLog[ORG] = catalogEvents();
    serverLog[OTHER_ORG] = [
      envelope(1, "evt-other-1", "drug_created", {
        drug_id: "22222222-2222-2222-2222-222222222222",
        organization_id: OTHER_ORG,
        name: "Other tenant drug",
        sku: null,
        unit_price: 1,
        unit_of_measure: "unit",
        reorder_level: 0,
        reorder_quantity: 0,
        is_active: true,
      }, OTHER_ORG),
    ];
    setCursor(ORG, 5);

    await pullAs(OTHER_ORG);

    // The other org's sync moved only its own cursor.
    expect(cursorFor(OTHER_ORG)).toBe(1);
    expect(cursorFor(ORG)).toBe(5);
  });

  it("starts a switched org at its own position, not the previous org's", async () => {
    serverLog[ORG] = catalogEvents();
    serverLog[OTHER_ORG] = [
      envelope(1, "evt-other-1", "drug_created", {
        drug_id: "22222222-2222-2222-2222-222222222222",
        organization_id: OTHER_ORG,
        name: "Other tenant drug",
        sku: null,
        unit_price: 1,
        unit_of_measure: "unit",
        reorder_level: 0,
        reorder_quantity: 0,
        is_active: true,
      }, OTHER_ORG),
    ];

    await pullAs(ORG);
    expect(cursorFor(ORG)).toBe(5);

    // Switching to the org with a single event must not carry over 5 and skip it.
    await pullAs(OTHER_ORG);
    expect(cursorFor(OTHER_ORG)).toBe(1);
  });

  it("ignores the legacy unscoped cursor instead of migrating its value", async () => {
    // A number that was never scoped to an org.
    rawDb().prepare("INSERT INTO sync_meta(key, value) VALUES(?, ?)").run("event_pull_seq", "225");
    serverLog[ORG] = catalogEvents();

    await pullAs(ORG);

    // The org's events were pulled, not skipped behind an unowned number.
    expect(drugCount()).toBe(5);
    // The legacy row is left exactly as it was.
    const legacy = rawDb()
      .prepare("SELECT value FROM sync_meta WHERE key = ?")
      .get("event_pull_seq") as { value: string };
    expect(legacy.value).toBe("225");
  });

  it("migrate_v30's reset of the legacy key is unchanged", async () => {
    rawDb().prepare("INSERT INTO sync_meta(key, value) VALUES(?, ?)").run("event_pull_seq", "99");
    const { migrate_v30, getDb } = await import("@/lib/localDb");
    await migrate_v30(await getDb());
    const legacy = rawDb()
      .prepare("SELECT value FROM sync_meta WHERE key = ?")
      .get("event_pull_seq") as { value: string };
    expect(legacy.value).toBe("0");
  });
});

describe("stalled health", () => {
  it("is stalled when the head is ahead of the cursor", async () => {
    // Server head moves past what this pull covered: events landed after the
    // page was computed, so the device is genuinely behind.
    serverLog[ORG] = catalogEvents();
    headOverride = 69;
    setCursor(ORG, 2);

    await pullAs(ORG);

    expect(syncEngine.syncHealth.pulledSeq).toBe(5);
    expect(syncEngine.syncHealth.serverHeadSeq).toBe(69);
    expect(syncEngine.syncHealth.stalled).toBe(true);
  });

  it("is not stalled once cursor equals head", async () => {
    serverLog[ORG] = catalogEvents();
    await pullAs(ORG);

    expect(syncEngine.syncHealth.pulledSeq).toBe(syncEngine.syncHealth.serverHeadSeq);
    expect(syncEngine.syncHealth.stalled).toBe(false);
  });

  it("does not report a false zero while the head is still unknown", async () => {
    // Server unreachable for the pull: head stays null, so no stall claim.
    const { syncApi } = await import("@/api/sync");
    vi.mocked(syncApi.pullEvents).mockRejectedValue(new Error("offline"));
    await pullAs(ORG);

    expect(syncEngine.syncHealth.serverHeadSeq).toBeNull();
    expect(syncEngine.syncHealth.stalled).toBe(false);
  });
});