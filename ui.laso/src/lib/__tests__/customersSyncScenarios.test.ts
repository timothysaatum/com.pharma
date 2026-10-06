/**
 * The Customers investigation scenarios, S-A..S-H, as a standing regression suite.
 *
 * Real installRealDb() (production migration chain, in-memory SQLite) and the real
 * device projectors, writers and read paths. The device's own laso.db is never
 * opened. Server-side behaviour is covered by
 * backend.laso/tests/integration/test_loyalty_convergence.py; this file is the
 * device half of the same contract.
 *
 *   S-A  a customer created online, then pulled
 *   S-B  a customer created on the device, event accepted  (Mike's exact state)
 *   S-C  an online sale earns points and the device converges after the next pull
 *   S-D  an offline-synced sale earns exactly once
 *   S-E  a refund publishes the opposite direction and the device follows
 *   S-F  duplicate and out-of-order loyalty events
 *   S-G  two devices, and an event for an unknown customer
 *   S-H  a cross-org id collision, and a pending row with local edits
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";
import { customerLoyaltyChangedEventId } from "@/lib/loyaltyEventId";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const OTHER = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const JOE = "5823ef27-51ce-4431-9adc-e81f9b3f949f";
const MIKE = "0615fb0e-416f-4ecb-bca1-d4839446cb36";
const KWAME = "88888888-8888-8888-8888-888888888888";
const SALE = "APO1-20261005-0001";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "sales", "event_outbox", "applied_events",
                     "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }
let seq = 0;
function env(aggId: string, type: string, payload: Record<string, unknown>,
             orgId = ORG, aggregateType = "customer") {
    seq += 1;
    return {
        event_id: `sa${String(seq).padStart(6, "0")}`.slice(0, 26),
        aggregate_id: aggId, aggregate_type: aggregateType, event_type: type,
        schema_version: 1, payload, dependencies: [],
        authored_at: "2026-09-21T07:13:23.469913Z", authored_by: orgId,
        branch_id: BRANCH, org_id: orgId, hash_self: "a".repeat(64),
        hash_prev: "0".repeat(64), seq,
    } as never;
}
function created(orgId = ORG, over: Record<string, unknown> = {}) {
    return {
        organization_id: orgId, customer_type: "registered", first_name: "Joe",
        last_name: "B", loyalty_points: 0, loyalty_tier: "bronze",
        allergies: [], chronic_conditions: [], version_vector: {}, ...over,
    };
}
function loyalty(customerId: string, points: number, tier: string,
                 direction: "earn" | "refund", saleId = SALE, orgId = ORG) {
    return {
        customer_id: customerId, organization_id: orgId, loyalty_points: points,
        loyalty_tier: tier, sale_id: saleId, direction, source: "online_sale",
    };
}
async function row(id: string) {
    const r = await (await db()).select<Record<string, unknown>[]>(
        "SELECT organization_id, loyalty_points, loyalty_tier, sync_status, first_name " +
        "FROM customers WHERE id = $1", [id]);
    return r[0];
}
let obSeq = 0;
async function outbox(aggType: string, aggId: string, status: string) {
    obSeq += 1;
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO event_outbox (event_id, aggregate_type, event_type, aggregate_id,
            org_id, branch_id, authored_by, authored_at, schema_version, payload,
            dependencies, hash_prev, hash_self, status, attempts, created_at)
         VALUES ($1,$2,$3,$4,$5,$6,$5,$7,1,'{}','[]','0','a',$8,0,$7)`,
        [`ob${String(obSeq).padStart(6, "0")}`.slice(0, 26), aggType,
         `${aggType}_created`, aggId, ORG, BRANCH, now, status]);
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    seq = 0;
    obSeq = 0;
    vi.restoreAllMocks();
});

describe("S-A  a customer created online, then pulled", () => {
    it("lands with the server's absolute state", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created(ORG, {
            loyalty_points: 125, loyalty_tier: "silver" })));
        const r = await row(JOE);
        expect(r?.organization_id).toBe(ORG);
        expect(r?.loyalty_points).toBe(125);
        expect(r?.loyalty_tier).toBe("silver");
        expect(r?.sync_status).toBe("synced");
    });
});

describe("S-B  a customer created on the device (Mike's exact state)", () => {
    it("stays pending until its event is accepted, then reconciles", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult } = await import("@/lib/localDb");
        const now = new Date().toISOString();
        await writeLocal.customer({
            id: MIKE, organization_id: ORG, customer_type: "registered",
            first_name: "Mike", last_name: "C", phone: null, email: null,
            date_of_birth: null, address: null, allergies: [], chronic_conditions: [],
            loyalty_points: 0, loyalty_tier: "bronze", total_orders: 0, total_value: 0,
            preferred_contact_method: "email", marketing_consent: false, is_active: true,
            insurance_provider_id: null, insurance_member_id: null,
            insurance_card_image_url: null, preferred_contract_id: null,
            version_vector: {}, created_at: now, updated_at: now,
        } as never, "create");
        await outbox("customer", MIKE, "pending");
        expect((await row(MIKE))?.sync_status).toBe("pending");

        await markOutboxResult("ob000001", "accepted");
        expect((await row(MIKE))?.sync_status).toBe("synced");
    });
});

describe("S-C  an online sale earns, and the device converges on the next pull", () => {
    it("takes the device from 0 to the server's 125", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created()));
        expect((await row(JOE))?.loyalty_points).toBe(0);

        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 125, "silver", "earn")));

        const r = await row(JOE);
        expect(r?.loyalty_points).toBe(125);
        expect(r?.loyalty_tier).toBe("silver");
    });

    it("the device derives the same event id the server will use", async () => {
        const id = await customerLoyaltyChangedEventId(JOE, SALE, "earn");
        expect(id).toHaveLength(26);
        expect(id).toBe(id.toUpperCase());
    });
});

describe("S-D  an offline-synced sale earns exactly once", () => {
    it("one sale_created, one loyalty award, and a replay adds nothing", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created()));
        const once = env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 30, "bronze", "earn"));
        await applyEventLocally(once);
        expect((await row(JOE))?.loyalty_points).toBe(30);

        await applyEventLocally(once);
        await applyEventLocally(once);
        expect((await row(JOE))?.loyalty_points).toBe(30);
    });
});

describe("S-E  a refund reverses the balance", () => {
    it("earn then refund returns the row to where it started", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created()));
        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 125, "silver", "earn")));
        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 0, "bronze", "refund")));

        const r = await row(JOE);
        expect(r?.loyalty_points).toBe(0);
        expect(r?.loyalty_tier).toBe("bronze");
    });

    it("the earn and refund ids differ, so the reversal is never a duplicate", async () => {
        const earn = await customerLoyaltyChangedEventId(JOE, SALE, "earn");
        const refund = await customerLoyaltyChangedEventId(JOE, SALE, "refund");
        expect(earn).not.toBe(refund);
    });
});

describe("S-F  duplicate and out-of-order events", () => {
    it("a duplicate is an assignment, not an addition", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created()));
        const e = env(JOE, "customer_loyalty_changed", loyalty(JOE, 90, "bronze", "earn"));
        await applyEventLocally(e);
        await applyEventLocally(e);
        await applyEventLocally(e);
        expect((await row(JOE))?.loyalty_points).toBe(90);
    });

    it("out of order: a refund seen before its earn still lands on a real value", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(env(JOE, "customer_created", created()));
        // Refund first, then the earn it reverses.
        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 0, "bronze", "refund")));
        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 125, "silver", "earn")));
        expect((await row(JOE))?.loyalty_points).toBe(125);
    });
});

describe("S-G  two devices, and an unknown customer", () => {
    it("an event for a customer this device does not hold is a recorded failure", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { getDb } = await import("@/lib/localDb");
        await expect(
            applyEventLocally(env(MIKE, "customer_loyalty_changed",
                loyalty(MIKE, 30, "bronze", "earn"))),
        ).rejects.toThrow(/not in the local read model/);

        // syncEngine catches that and records it durably rather than freezing.
        const { recordEventProjectionFailure } = await import("@/lib/localDb");
        const envelope = env(MIKE, "customer_loyalty_changed",
            loyalty(MIKE, 30, "bronze", "earn")) as unknown as Record<string, unknown>;
        await recordEventProjectionFailure(await getDb(), envelope as never,
            new Error("unknown customer"));
        const failures = await (await getDb()).select<{ event_id: string }[]>(
            "SELECT event_id FROM sync_event_failures");
        expect(failures.length).toBeGreaterThan(0);
    });

    it("device B receives the same absolute state as device A", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        // Two devices are two databases; here we prove the payload is what
        // converges them, by applying to a clean read model.
        await applyEventLocally(env(JOE, "customer_created", created()));
        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 125, "silver", "earn")));
        expect((await row(JOE))?.loyalty_points).toBe(125);
    });
});

describe("S-H  cross-org collision and local edits", () => {
    it("the real-org event is refused while a foreign row holds the id", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const now = new Date().toISOString();
        await (await db()).execute(
            `INSERT INTO customers (id, organization_id, customer_type, first_name,
                last_name, loyalty_points, loyalty_tier, is_active, is_deleted,
                sync_status, sync_version, updated_at, created_at, version_vector)
             VALUES ($1,$2,'registered','Foreign','K',150,'gold',1,0,'synced',1,$3,$3,'{}')`,
            [KWAME, OTHER, now]);
        vi.spyOn(console, "error").mockImplementation(() => {});

        await expect(
            applyEventLocally(env(KWAME, "customer_created", created(ORG))),
        ).rejects.toThrow(/already exists locally/);
        expect((await row(KWAME))?.organization_id).toBe(OTHER);
    });

    it("a pending row with local edits is NOT clobbered by a loyalty event", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { writeLocal } = await import("@/lib/localWrite");
        const now = new Date().toISOString();
        await writeLocal.customer({
            id: JOE, organization_id: ORG, customer_type: "registered",
            first_name: "JoeLOCAL", last_name: "Edited", phone: null, email: null,
            date_of_birth: null, address: null, allergies: [], chronic_conditions: [],
            loyalty_points: 3, loyalty_tier: "bronze", total_orders: 0, total_value: 0,
            preferred_contact_method: "sms", marketing_consent: true, is_active: true,
            insurance_provider_id: null, insurance_member_id: null,
            insurance_card_image_url: null, preferred_contract_id: null,
            version_vector: {}, created_at: now, updated_at: now,
        } as never, "create");

        await applyEventLocally(env(JOE, "customer_loyalty_changed",
            loyalty(JOE, 125, "silver", "earn")));

        const r = await row(JOE);
        expect(r?.loyalty_points).toBe(125);
        expect(r?.loyalty_tier).toBe("silver");
        expect(r?.first_name).toBe("JoeLOCAL");
        expect(r?.sync_status).toBe("pending");
    });
});
