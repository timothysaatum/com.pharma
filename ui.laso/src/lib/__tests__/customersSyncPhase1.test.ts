/**
 * Customers sync regression suite — Phase 1 (device hardening).
 *
 * Real installRealDb() (production migration chain, in-memory SQLite). The
 * device's own laso.db is never opened.
 *
 * Covers:
 *   P1-1  searchCustomers / searchCustomerMatches fail closed without an org
 *   P1-2  _customerCreated rejects a payload/envelope org mismatch
 *   P1-2  _customerCreated shouts on a same-id, different-org collision
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const FOREIGN = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const KWAME = "88888888-8888-8888-8888-888888888888";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "sales", "event_outbox", "applied_events",
                     "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }

let seq = 0;
function env(aggId: string, eventType: string, payload: Record<string, unknown>,
             envelopeOrg: string, aggregateType = "customer") {
    seq += 1;
    return {
        event_id: `p1${String(seq).padStart(6, "0")}`.slice(0, 26),
        aggregate_id: aggId,
        aggregate_type: aggregateType,
        event_type: eventType,
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: "2026-09-21T07:13:23.469913Z",
        authored_by: envelopeOrg,
        branch_id: BRANCH,
        org_id: envelopeOrg,
        hash_self: "a".repeat(64),
        hash_prev: "0".repeat(64),
        seq,
    } as never;
}
function custPayload(org: string, over: Record<string, unknown> = {}) {
    return {
        organization_id: org,
        customer_type: "registered",
        first_name: "Joe",
        last_name: "B",
        loyalty_points: 0,
        loyalty_tier: "bronze",
        allergies: [],
        chronic_conditions: [],
        version_vector: {},
        ...over,
    };
}
async function row(id: string) {
    const r = await (await db()).select<Record<string, unknown>[]>(
        "SELECT organization_id, first_name, loyalty_points, loyalty_tier, sync_status " +
        "FROM customers WHERE id = $1",
        [id],
    );
    return r[0];
}
async function editLocal(id: string, name: string) {
    await (await db()).execute(
        "UPDATE customers SET first_name = $1 WHERE id = $2", [name, id]);
}

async function insertLocal(id: string, org: string, name = "Local") {
    const now = new Date().toISOString();
    await (await db()).execute(
        "INSERT INTO customers (id, organization_id, customer_type, first_name, " +
        "last_name, loyalty_points, loyalty_tier, is_active, is_deleted, sync_status, " +
        "sync_version, updated_at, created_at, version_vector) " +
        "VALUES ($1,$2,'registered',$3,'X',0,'bronze',1,0,'pending',1,$4,$4,'{}')",
        [id, org, name, now],
    );
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    seq = 0;
    vi.restoreAllMocks();
});

describe("P1-1  customer reads fail closed without an organization", () => {
    it("searchCustomers returns no rows and warns when organization_id is missing", async () => {
        await insertLocal(KWAME, ORG, "RealOrgRow");
        await insertLocal("aaaaaaaa-0000-0000-0000-000000000009", FOREIGN, "ForeignRow");

        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const { localRead } = await import("@/lib/localRead");

        const noParam = await localRead.searchCustomers({} as never);
        const undef = await localRead.searchCustomers(
            { organization_id: undefined } as never);
        const empty = await localRead.searchCustomers({ organization_id: "" });

        expect(noParam.customers).toEqual([]);
        expect(noParam.total).toBe(0);
        expect(undef.customers).toEqual([]);
        expect(empty.customers).toEqual([]);
        expect(warn).toHaveBeenCalled();
        expect(warn.mock.calls[0][0]).toContain("organization_id is required");
    });

    it("searchCustomers still returns the caller's own rows", async () => {
        await insertLocal(KWAME, ORG, "RealOrgRow");
        await insertLocal("aaaaaaaa-0000-0000-0000-000000000009", FOREIGN, "ForeignRow");

        const { localRead } = await import("@/lib/localRead");
        const scoped = await localRead.searchCustomers({ organization_id: ORG });

        expect(scoped.total).toBe(1);
        expect(scoped.customers[0].first_name).toBe("RealOrgRow");
    });

    it("searchCustomerMatches fails closed without an organization", async () => {
        await insertLocal(KWAME, ORG, "Joe");
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const { localRead } = await import("@/lib/localRead");

        const unscoped = await localRead.searchCustomerMatches("joe", 10);
        expect(unscoped).toEqual([]);
        expect(warn.mock.calls[0][0]).toContain("organization_id is required");

        const scoped = await localRead.searchCustomerMatches("joe", 10, ORG);
        expect(scoped).toHaveLength(1);
    });
});

describe("P1-2  _customerCreated refuses cross-org and colliding events", () => {
    it("rejects a payload/envelope organization mismatch without inserting", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");

        // Envelope says the real org; payload claims the E2E sentinel org.
        await expect(
            applyEventLocally(
                env("bbbbbbbb-0000-0000-0000-000000000001", "customer_created",
                    custPayload(FOREIGN, { first_name: "Phantom" }), ORG),
            ),
        ).rejects.toThrow(/organization/i);

        expect(await row("bbbbbbbb-0000-0000-0000-000000000001")).toBeUndefined();
    });

    it("always writes the ENVELOPE organization, not the payload's", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const id = "bbbbbbbb-0000-0000-0000-000000000002";

        // Both agree on ORG: the row must land under ORG.
        await applyEventLocally(
            env(id, "customer_created", custPayload(ORG, { first_name: "Agrees" }), ORG),
        );
        expect((await row(id))?.organization_id).toBe(ORG);
    });

    it("records a loud failure when the id exists locally under a different org", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { recordEventProjectionFailure } = await import("@/lib/localDb");

        // The Kwame case: a foreign-org row already occupies this id.
        await insertLocal(KWAME, FOREIGN, "ForeignKwame");
        const failure = vi.spyOn(console, "error").mockImplementation(() => {});

        await expect(
            applyEventLocally(
                env(KWAME, "customer_created", custPayload(ORG, { first_name: "RealKwame" }), ORG),
            ),
        ).rejects.toThrow(/already exists locally\s+under organization/i);

        // The existing row must be untouched.
        const after = await row(KWAME);
        expect(after?.organization_id).toBe(FOREIGN);
        expect(after?.first_name).toBe("ForeignKwame");
        expect(failure).toHaveBeenCalled();
        void recordEventProjectionFailure;
    });

    it("still accepts a replay for an id that already exists in the SAME org", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const id = "bbbbbbbb-0000-0000-0000-000000000003";

        await applyEventLocally(
            env(id, "customer_created", custPayload(ORG, { first_name: "First" }), ORG),
        );
        await editLocal(id, "LocalEdit");

        // Same org: INSERT OR IGNORE semantics, no throw, local row preserved.
        await applyEventLocally(
            env(id, "customer_created", custPayload(ORG, { first_name: "Second" }), ORG),
        );
        expect((await row(id))?.first_name).toBe("LocalEdit");
    });
});
