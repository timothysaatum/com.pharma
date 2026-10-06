/**
 * Phase 3.4 — the device projector for customer_loyalty_changed.
 *
 * The load-bearing property: it writes the balance and NOTHING else. A customer
 * row on a `pending` sync_status carrying offline edits must come through a
 * loyalty event with its name, contact details and consent intact.
 *
 * Real installRealDb(). The device's own laso.db is never opened.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const OTHER = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const JOE = "5823ef27-51ce-4431-9adc-e81f9b3f949f";
const MIKE = "0615fb0e-416f-4ecb-bca1-d4839446cb36";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "sales", "event_outbox", "applied_events",
                     "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }

let seq = 0;
async function addCustomer(id: string, over: Record<string, unknown> = {}) {
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO customers (id, organization_id, customer_type, first_name, last_name,
            phone, email, allergies, chronic_conditions, preferred_contact_method,
            marketing_consent, loyalty_points, loyalty_tier, is_active, is_deleted,
            sync_status, sync_version, updated_at, created_at, version_vector)
         VALUES ($1,$2,'registered','Joe','LOCAL',NULL,NULL,'[]','[]','sms',1,
                 7,'bronze',1,0,'pending',1,$3,$3,'{}')`,
        [id, ORG, now],
    );
    void over;
}
async function snapshot(id: string) {
    const r = await (await db()).select<Record<string, unknown>[]>(
        `SELECT organization_id, first_name, last_name, loyalty_points, loyalty_tier,
                preferred_contact_method, marketing_consent, sync_status, is_active
           FROM customers WHERE id = $1`, [id]);
    return r[0];
}
function loyaltyEnv(customerId: string, points: number, tier: string,
                    direction: "earn" | "refund" = "earn", envelopeOrg = ORG) {
    seq += 1;
    return {
        event_id: `ly${String(seq).padStart(6, "0")}`.slice(0, 26),
        aggregate_id: customerId,
        aggregate_type: "customer",
        event_type: "customer_loyalty_changed",
        schema_version: 1,
        payload: {
            customer_id: customerId, organization_id: envelopeOrg,
            loyalty_points: points, loyalty_tier: tier,
            sale_id: "APO1-20261005-0001", direction,
        },
        dependencies: [], authored_at: "2026-10-06T00:00:00Z",
        authored_by: envelopeOrg, branch_id: BRANCH, org_id: envelopeOrg,
        hash_self: "a".repeat(64), hash_prev: "0".repeat(64), seq,
    } as never;
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    seq = 0;
    vi.restoreAllMocks();
});

describe("P3.4  customer_loyalty_changed on the device", () => {
    it("writes the absolute balance onto a pending row without touching local edits", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");

        await applyEventLocally(loyaltyEnv(JOE, 125, "silver"));

        const row = await snapshot(JOE);
        expect(row.loyalty_points).toBe(125);
        expect(row.loyalty_tier).toBe("silver");
        // Everything the user edited offline must survive.
        expect(row.first_name).toBe("Joe");
        expect(row.last_name).toBe("LOCAL");
        expect(row.preferred_contact_method).toBe("sms");
        expect(row.marketing_consent).toBe(1);
        // sync_status is deliberately NOT flipped: this device still has unsent work.
        expect(row.sync_status).toBe("pending");
        expect(row.is_active).toBe(1);
    });

    it("is idempotent - absolute state, applied twice, same result", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const e = loyaltyEnv(JOE, 125, "silver");
        await applyEventLocally(e);
        await applyEventLocally(e);
        expect((await snapshot(JOE)).loyalty_points).toBe(125);
    });

    it("handles a refund arriving after its earn", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(loyaltyEnv(JOE, 125, "silver", "earn"));
        expect((await snapshot(JOE)).loyalty_points).toBe(125);
        await applyEventLocally(loyaltyEnv(JOE, 0, "bronze", "refund"));
        const row = await snapshot(JOE);
        expect(row.loyalty_points).toBe(0);
        expect(row.loyalty_tier).toBe("bronze");
    });

    it("records a failure for a customer this device does not have", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await expect(
            applyEventLocally(loyaltyEnv(MIKE, 30, "bronze")),
        ).rejects.toThrow(/not in the local read model/);
        expect(await snapshot(MIKE)).toBeUndefined();
    });

    it("never creates the customer row", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await expect(applyEventLocally(loyaltyEnv(MIKE, 30, "bronze")))
            .rejects.toThrow();
        const n = await (await db()).select<{ n: number }[]>("SELECT COUNT(*) AS n FROM customers");
        expect(n[0].n).toBe(0);
    });

    it("refuses to apply across organizations", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await expect(
            applyEventLocally(loyaltyEnv(JOE, 999, "platinum", "earn", OTHER)),
        ).rejects.toThrow(/across organizations/);
        expect((await snapshot(JOE)).loyalty_points).toBe(7);
    });

    it("rejects a payload missing the balance", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const bad = {
            ...(loyaltyEnv(JOE, 10, "bronze") as unknown as Record<string, unknown>),
            payload: { customer_id: JOE },
        };
        await expect(applyEventLocally(bad as never)).rejects.toThrow(/must carry/);
    });

    it("emits customers:changed so the page and typeahead refresh", async () => {
        await addCustomer(JOE);
        const { appEvents } = await import("@/lib/events");
        const seen = vi.fn();
        const off = appEvents.on("customers:changed", seen);
        const { applyEventLocally } = await import("@/lib/localProjectors");

        await applyEventLocally(loyaltyEnv(JOE, 125, "silver"));
        expect(seen).toHaveBeenCalledTimes(1);
        off();
    });

    it("matches only on id AND organization_id", async () => {
        await addCustomer(JOE);
        const { applyEventLocally } = await import("@/lib/localProjectors");
        // Same numeric balance, but for a different org: must not touch our row.
        await expect(
            applyEventLocally(loyaltyEnv(JOE, 500, "gold", "earn", OTHER)),
        ).rejects.toThrow(/across organizations/);
        expect((await snapshot(JOE)).loyalty_points).toBe(7);
    });
});
