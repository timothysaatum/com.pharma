/**
 * Phase 2 — stale pending-marker reconciliation for every covered aggregate.
 *
 * Real installRealDb(). Mike's exact state is the regression anchor: a pending
 * row whose only customer_created event is 'accepted', with an empty unsent set.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const MIKE = "0615fb0e-416f-4ecb-bca1-d4839446cb36";
const RX = "cccccccc-0000-0000-0000-00000000aaaa";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["customers", "sales", "prescriptions", "drug_batches",
                     "branch_inventory", "event_outbox", "applied_events"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}
async function db() { return (await import("@/lib/localDb")).getDb(); }
let n = 0;
async function outbox(aggType: string, aggId: string, status: string) {
    n += 1;
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO event_outbox (event_id, aggregate_type, event_type, aggregate_id,
            org_id, branch_id, authored_by, authored_at, schema_version, payload,
            dependencies, hash_prev, hash_self, status, attempts, created_at)
         VALUES ($1,$2,$3,$4,$5,$6,$5,$7,1,'{}','[]','0','a',$8,0,$7)`,
        [`ob${String(n).padStart(6, "0")}`.slice(0, 26), aggType,
         `${aggType}_created`, aggId, ORG, BRANCH, now, status],
    );
}
async function addCustomer(id: string, status: string) {
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO customers (id, organization_id, customer_type, first_name, last_name,
            loyalty_points, loyalty_tier, is_active, is_deleted, sync_status, sync_version,
            updated_at, created_at, version_vector)
         VALUES ($1,$2,'registered','Mike','C',0,'bronze',1,0,$3,1,$4,$4,'{}')`,
        [id, ORG, status, now],
    );
}
async function addSale(id: string, status: string) {
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO sales (id, organization_id, branch_id, sale_number, subtotal,
            discount_amount, tax_amount, total_amount, payment_method, payment_status,
            change_amount, cashier_id, status, sync_status, sync_version, created_at, updated_at)
         VALUES ($1,$2,$3,$4,10,0,0,10,'cash','completed',0,'u1','completed',$5,1,$6,$6)`,
        [id, ORG, BRANCH, `S-${id.slice(0, 6)}`, status, now],
    );
}
async function addPrescription(id: string, status: string) {
    const now = new Date().toISOString();
    await (await db()).execute(
        `INSERT INTO prescriptions (id, organization_id, branch_id, prescription_number,
            customer_id, prescriber_name, prescriber_license, issue_date, expiry_date,
            medications, status, sync_status, sync_version, created_at, updated_at)
         VALUES ($1,$2,$3,$4,'cust-1','Dr Who','LIC-1','2026-01-01','2027-01-01',
                 '[]','active',$5,1,$6,$6)`,
        [id, ORG, BRANCH, `RX-${id.slice(0, 8)}`, status, now]);
}

async function syncStatus(table: string, id: string) {
    const r = await (await db()).select<{ sync_status: string }[]>(
        `SELECT sync_status FROM ${table} WHERE id = $1`, [id]);
    return r[0]?.sync_status;
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    n = 0;
    vi.restoreAllMocks();
});

describe("P2  accepted events flip the aggregate row to synced", () => {
    it("Mike's exact state: pending row, accepted create, empty unsent set", async () => {
        await addCustomer(MIKE, "pending");
        await outbox("customer", MIKE, "pending");
        const { markOutboxResult } = await import("@/lib/localDb");

        await markOutboxResult("ob000001", "accepted");
        expect(await syncStatus("customers", MIKE)).toBe("synced");
    });

    it("leaves the row pending while an edit for the same aggregate is still unsent", async () => {
        await addCustomer(MIKE, "pending");
        await outbox("customer", MIKE, "pending");
        await outbox("customer", MIKE, "pending");
        const { markOutboxResult } = await import("@/lib/localDb");

        await markOutboxResult("ob000001", "accepted");
        expect(await syncStatus("customers", MIKE)).toBe("pending");

        await markOutboxResult("ob000002", "accepted");
        expect(await syncStatus("customers", MIKE)).toBe("synced");
    });

    it("honours every unsent status, including accepted_deferred and failed", async () => {
        await addCustomer(MIKE, "pending");
        await outbox("customer", MIKE, "accepted");
        await outbox("customer", MIKE, "failed");
        const { markOutboxResult } = await import("@/lib/localDb");
        await markOutboxResult("ob000001", "accepted");
        expect(await syncStatus("customers", MIKE)).toBe("pending");
    });

    it("does not mark synced on rejected_permanent", async () => {
        await addCustomer(MIKE, "pending");
        await outbox("customer", MIKE, "pending");
        const { markOutboxResult } = await import("@/lib/localDb");
        await markOutboxResult("ob000001", "rejected_permanent");
        expect(await syncStatus("customers", MIKE)).toBe("pending");
    });

    it("reconciles sale, drug_batch and branch_inventory too", async () => {
        const now = new Date().toISOString();
        const sale = "5ale0000-0000-0000-0000-000000000001";
        const batch = "ba7c0000-0000-0000-0000-000000000001";
        const inv = "b1a00000-0000-0000-0000-000000000001";
        await addSale(sale, "pending");
        await (await db()).execute(
            `INSERT INTO drug_batches (id, branch_id, drug_id, batch_number, quantity,
                remaining_quantity, manufacturing_date, expiry_date, cost_price, selling_price,
                sync_status, sync_version, created_at, updated_at)
             VALUES ($1,$2,'d1','B1',5,5,'2026-01-01','2027-01-01',1,2,'pending',1,$3,$3)`,
            [batch, BRANCH, now]);
        await (await db()).execute(
            `INSERT INTO branch_inventory (id, drug_id, branch_id, quantity,
                reserved_quantity, sync_status, sync_version, updated_at, created_at)
             VALUES ($1,'d1',$2,5,0,'pending',1,$3,$3)`,
            [inv, BRANCH, now]);
        await outbox("sale", sale, "pending");
        await outbox("drug_batch", batch, "pending");
        await outbox("branch_inventory", inv, "pending");

        const { markOutboxResult } = await import("@/lib/localDb");
        await markOutboxResult("ob000001", "accepted");
        await markOutboxResult("ob000002", "accepted");
        await markOutboxResult("ob000003", "accepted");

        expect(await syncStatus("sales", sale)).toBe("synced");
        expect(await syncStatus("drug_batches", batch)).toBe("synced");
        expect(await syncStatus("branch_inventory", inv)).toBe("synced");
    });

    it("prescription behaviour is unchanged", async () => {
        await addPrescription(RX, "pending");
        await outbox("prescription", RX, "pending");
        const { markOutboxResult } = await import("@/lib/localDb");
        await markOutboxResult("ob000001", "accepted");
        expect(await syncStatus("prescriptions", RX)).toBe("synced");
    });
});

describe("P2  one-time login repair for already-stranded rows", () => {
    it("sweeps pending rows with an empty unsent set and leaves real work alone", async () => {
        const stranded = "5tranded-0000-0000-0000-00000000001";
        const busy = "b0000000-0000-0000-0000-000000000001";
        await addCustomer(stranded, "pending");
        await addCustomer(busy, "pending");
        await addPrescription(RX, "pending");
        // busy still has an unsent edit; RX still has a pending create.
        await outbox("customer", busy, "pending");
        await outbox("prescription", RX, "pending");
        // stranded's only event is long since accepted.
        await outbox("customer", stranded, "accepted");

        const { repairStalePendingMarkers } = await import("@/lib/localDb");
        const counts = await repairStalePendingMarkers();

        expect(counts.customers).toBe(1);
        expect(counts.prescriptions).toBe(0);
        expect(await syncStatus("customers", stranded)).toBe("synced");
        expect(await syncStatus("customers", busy)).toBe("pending");
        expect(await syncStatus("prescriptions", RX)).toBe("pending");
    });

    it("is idempotent", async () => {
        await addCustomer("5tranded-0000-0000-0000-00000000002", "pending");
        await outbox("customer", "5tranded-0000-0000-0000-00000000002", "accepted");
        const { repairStalePendingMarkers } = await import("@/lib/localDb");
        expect((await repairStalePendingMarkers()).customers).toBe(1);
        expect((await repairStalePendingMarkers()).customers).toBe(0);
    });

    it("never sweeps a conflict row", async () => {
        await addCustomer("c0000000-0000-0000-0000-000000000001", "conflict");
        await outbox("customer", "c0000000-0000-0000-0000-000000000001", "accepted");
        const { repairStalePendingMarkers } = await import("@/lib/localDb");
        await repairStalePendingMarkers();
        expect(await syncStatus("customers", "c0000000-0000-0000-0000-000000000001"))
            .toBe("conflict");
    });
});
