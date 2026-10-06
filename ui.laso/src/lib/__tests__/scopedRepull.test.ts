/**
 * Optional one-shot scoped re-pull.
 *
 * The whole point is that it does NOT move the stored cursor: a device that has
 * already pulled past seq 25 must be able to replay the customer history that
 * its cursor skipped, without losing its position in the log.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const FOREIGN = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const KWAME = "88888888-8888-8888-8888-888888888888";

const pullEvents = vi.fn();

vi.mock("@/api/sync", () => ({
    syncApi: {
        pullEvents: (...args: unknown[]) => pullEvents(...args),
        pushEvents: vi.fn(),
    },
}));

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "event_outbox", "applied_events", "sales",
                     "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }
async function row(id: string) {
    const r = await (await db()).select<Record<string, unknown>[]>(
        "SELECT organization_id, loyalty_points FROM customers WHERE id = $1", [id]);
    return r[0];
}
function kwameEnvelope(seq: number) {
    return {
        event_id: `rp${String(seq).padStart(6, "0")}`.slice(0, 26),
        aggregate_id: KWAME, aggregate_type: "customer",
        event_type: "customer_created", schema_version: 1,
        payload: {
            organization_id: ORG, customer_type: "registered", first_name: "Kwame",
            last_name: "N", loyalty_points: 150, loyalty_tier: "gold",
            allergies: [], chronic_conditions: [], version_vector: {},
        },
        dependencies: [], authored_at: "2026-09-21T07:13:23.469913Z",
        authored_by: ORG, branch_id: BRANCH, org_id: ORG,
        hash_self: "a".repeat(64), hash_prev: "0".repeat(64), seq,
    };
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    pullEvents.mockReset();
});

describe("scoped one-shot re-pull", () => {
    it("replays customer history from seq 0 without moving the stored cursor", async () => {
        const { setEventPullSeq, getEventPullSeq } = await import("@/lib/localDb");
        await setEventPullSeq(104, ORG);

        pullEvents.mockResolvedValueOnce({
            events: [kwameEnvelope(25)], has_more: false, next_after_seq: 25,
            server_head_seq: 104,
        });
        const { repullAggregateOnce } = await import("@/lib/syncEngine");
        const applied = await repullAggregateOnce("customer", ORG);

        expect(applied).toBe(1);
        expect(pullEvents).toHaveBeenCalledWith(0, 200, ["customer"]);
        expect((await row(KWAME))?.organization_id).toBe(ORG);
        // The cursor is exactly where it was.
        expect(await getEventPullSeq(ORG)).toBe(104);
    });

    it("requests only the named aggregate type", async () => {
        pullEvents.mockResolvedValue({
            events: [], has_more: false, next_after_seq: 0, server_head_seq: 0,
        });
        const { repullAggregateOnce } = await import("@/lib/syncEngine");
        await repullAggregateOnce("prescription", ORG);
        expect(pullEvents).toHaveBeenCalledWith(0, 200, ["prescription"]);
    });

    it("pages until the server says there is no more", async () => {
        pullEvents
            .mockResolvedValueOnce({
                events: [kwameEnvelope(25)], has_more: true, next_after_seq: 25,
                server_head_seq: 104,
            })
            .mockResolvedValueOnce({
                events: [], has_more: false, next_after_seq: 0, server_head_seq: 104,
            });
        const { repullAggregateOnce } = await import("@/lib/syncEngine");
        expect(await repullAggregateOnce("customer", ORG)).toBe(1);
        expect(pullEvents).toHaveBeenCalledTimes(2);
        expect(pullEvents).toHaveBeenLastCalledWith(25, 200, ["customer"]);
    });

    it("skips envelopes belonging to another organization", async () => {
        pullEvents.mockResolvedValueOnce({
            events: [{ ...kwameEnvelope(4), org_id: FOREIGN }], has_more: false,
            next_after_seq: 4, server_head_seq: 104,
        });
        const { repullAggregateOnce } = await import("@/lib/syncEngine");
        expect(await repullAggregateOnce("customer", ORG)).toBe(0);
        expect(await row(KWAME)).toBeUndefined();
    });

    it("survives an envelope that cannot be applied", async () => {
        const now = new Date().toISOString();
        await (await db()).execute(
            "INSERT INTO customers (id, organization_id, customer_type, first_name, " +
            "last_name, loyalty_points, loyalty_tier, is_active, is_deleted, sync_status, " +
            "sync_version, updated_at, created_at, version_vector) " +
            "VALUES ($1,$2,'registered','Foreign','K',0,'bronze',1,0,'synced',1,$3,$3,'{}')",
            [KWAME, FOREIGN, now],
        );
        pullEvents.mockResolvedValueOnce({
            events: [kwameEnvelope(25)], has_more: false, next_after_seq: 25,
            server_head_seq: 104,
        });
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        vi.spyOn(console, "info").mockImplementation(() => {});

        const { repullAggregateOnce } = await import("@/lib/syncEngine");
        expect(await repullAggregateOnce("customer", ORG)).toBe(0);
        expect(warn).toHaveBeenCalled();
        // The poisoned row is left intact for the v37 cleanup to remove.
        expect((await row(KWAME))?.organization_id).toBe(FOREIGN);
    });
});
