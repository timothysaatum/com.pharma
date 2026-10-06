/**
 * Migration v37 — one-time removal of customers belonging to another org.
 *
 * Real installRealDb(). The device's own laso.db is never opened.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const FOREIGN = "11111111-1111-1111-1111-111111111111";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const KWAME = "88888888-8888-8888-8888-888888888888";
const JOE = "5823ef27-51ce-4431-9adc-e81f9b3f949f";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "event_outbox", "applied_events",
                     "sales", "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }

let n = 0;
async function addCustomer(id: string, org: string, name = "X") {
    const now = new Date().toISOString();
    await (await db()).execute(
        "INSERT INTO customers (id, organization_id, customer_type, first_name, last_name, " +
        "loyalty_points, loyalty_tier, is_active, is_deleted, sync_status, sync_version, " +
        "updated_at, created_at, version_vector) " +
        "VALUES ($1,$2,'registered',$3,'X',0,'bronze',1,0,'synced',1,$4,$4,'{}')",
        [id, org, name, now],
    );
}
async function addOutbox(aggregateId: string, status: string) {
    n += 1;
    const now = new Date().toISOString();
    await (await db()).execute(
        "INSERT INTO event_outbox (event_id, aggregate_type, event_type, aggregate_id, " +
        "org_id, branch_id, authored_by, authored_at, schema_version, payload, dependencies, " +
        "hash_prev, hash_self, status, attempts, created_at) " +
        "VALUES ($1,'customer','customer_created',$2,$3,$4,$3,$5,1,'{}','[]','0','a',$6,0,$5)",
        [`ob${String(n).padStart(6, "0")}`.slice(0, 26), aggregateId, ORG, BRANCH, now, status],
    );
}
async function ids() {
    const r = await (await db()).select<{ id: string }[]>("SELECT id FROM customers ORDER BY id");
    return r.map((x) => x.id);
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    n = 0;
    vi.restoreAllMocks();
});

describe("v37 cross-org customer cleanup", () => {
    it("removes foreign rows and keeps every row of the signed-in org", async () => {
        const info = vi.spyOn(console, "info").mockImplementation(() => {});
        for (let i = 0; i < 49; i += 1) {
            await addCustomer(`f0000000-0000-0000-0000-${String(i).padStart(12, "0")}`, FOREIGN);
        }
        await addCustomer(JOE, ORG);
        await addCustomer(KWAME, ORG);

        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(49);

        const left = await ids();
        expect(left).toHaveLength(2);
        expect(left).toContain(JOE);
        expect(left).toContain(KWAME);
        expect(left.some((id) => id.startsWith("f0000000"))).toBe(false);
        void info;
    });

    it("removes the Kwame foreign row, which is what unblocks the real-org event", async () => {
        await addCustomer(KWAME, FOREIGN, "ForeignKwame");
        await addCustomer(JOE, ORG);

        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(1);
        expect(await ids()).toEqual([JOE]);

        // With the poisoned row gone, the projector can now insert the real-org
        // event that used to be swallowed by INSERT OR IGNORE.
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally({
            event_id: "realf000000000000000000000".slice(0, 26), aggregate_id: KWAME,
            aggregate_type: "customer", event_type: "customer_created", schema_version: 1,
            payload: {
                organization_id: ORG, customer_type: "registered", first_name: "Kwame",
                last_name: "N", loyalty_points: 150, loyalty_tier: "gold",
                allergies: [], chronic_conditions: [], version_vector: {},
            },
            dependencies: [], authored_at: "2026-09-21T07:13:23.469913Z", authored_by: ORG,
            branch_id: BRANCH, org_id: ORG, hash_self: "a".repeat(64),
            hash_prev: "0".repeat(64), seq: 25,
        } as never);

        const r = await (await db()).select<Record<string, unknown>[]>(
            "SELECT organization_id, first_name, loyalty_points FROM customers WHERE id = $1",
            [KWAME],
        );
        expect(r[0].organization_id).toBe(ORG);
        expect(r[0].loyalty_points).toBe(150);
    });

    it("keeps a foreign row that still has an unsent outbox event", async () => {
        await addCustomer(KWAME, FOREIGN, "HasUnsentWork");
        await addCustomer(JOE, ORG);
        await addOutbox(KWAME, "pending");

        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(0);
        expect(await ids()).toEqual(expect.arrayContaining([KWAME, JOE]));
    });

    it("keeps a foreign row for every unsent status, and removes one already synced", async () => {
        await addCustomer("a0000000-0000-0000-0000-000000000001", FOREIGN);
        await addOutbox("a0000000-0000-0000-0000-000000000001", "failed");
        await addCustomer("a0000000-0000-0000-0000-000000000002", FOREIGN);
        await addOutbox("a0000000-0000-0000-0000-000000000002", "accepted_deferred");
        await addCustomer("a0000000-0000-0000-0000-000000000003", FOREIGN);
        await addOutbox("a0000000-0000-0000-0000-000000000003", "synced");

        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(1);

        const left = await ids();
        expect(left).toContain("a0000000-0000-0000-0000-000000000001");
        expect(left).toContain("a0000000-0000-0000-0000-000000000002");
        expect(left).not.toContain("a0000000-0000-0000-0000-000000000003");
    });

    it("is idempotent: a second run removes nothing", async () => {
        await addCustomer(KWAME, FOREIGN);
        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(1);
        expect(await repairCrossOrgCustomers(ORG)).toBe(0);
        expect(await repairCrossOrgCustomers(ORG)).toBe(0);
    });

    it("never touches the current org's rows even when they are pending", async () => {
        await addCustomer(JOE, ORG);
        const now = new Date().toISOString();
        await (await db()).execute("UPDATE customers SET sync_status='pending' WHERE id=$1", [JOE]);

        const { repairCrossOrgCustomers } = await import("@/lib/localDb");
        expect(await repairCrossOrgCustomers(ORG)).toBe(0);
        expect(await ids()).toEqual([JOE]);
        const r = await (await db()).select<{ sync_status: string }[]>(
            "SELECT sync_status FROM customers WHERE id=$1", [JOE]);
        expect(r[0].sync_status).toBe("pending");
        void now;
    });
});
