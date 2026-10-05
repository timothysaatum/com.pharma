/**
 * The derived "dispensed N" figure must be identical online and offline.
 *
 * WHY PARITY IS THE POINT
 * -----------------------
 * `refills_remaining` is a stored counter and it drifted: the server never counted
 * an offline dispense, so the same prescription showed different numbers
 * depending on which side answered. That is P1.
 *
 * `dispensed_count` replaces it for display by counting completed sales, which is
 * the one thing every path agrees on. That is only an improvement if BOTH paths
 * compute it the same way — so this file pins the semantics, and the server test
 * next door pins the server's half against the same rules.
 *
 * Real installRealDb() (production migration chain, in-memory). The device's own
 * laso.db is never opened.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const RX = "dddddddd-1111-2222-3333-444444444444";
const RX_UNUSED = "dddddddd-1111-2222-3333-999999999999";
const CUSTOMER = "cccccccc-1111-2222-3333-444444444444";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["prescriptions", "sales", "customers", "drug_batches", "branch_inventory"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}

function seedPrescription(id: string, number: string) {
    rawDb()
        .prepare(
            `INSERT INTO prescriptions
               (id, organization_id, branch_id, prescription_number, customer_id,
                prescriber_name, prescriber_license, issue_date, expiry_date,
                medications, refills_allowed, refills_remaining, status,
                sync_status, sync_version, updated_at, created_at)
             VALUES (?,?,?,?,?,'Dr. Test','MED-1','2026-10-01','2026-12-01','[]',3,3,
                'active','synced',1,'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')`
        )
        .run(id, ORG, BRANCH, number, CUSTOMER);
}

function seedSale(rxId: string | null, status: string, n: number) {
    rawDb()
        .prepare(
            `INSERT INTO sales
               (id, organization_id, branch_id, sale_number, customer_id, customer_name,
                subtotal, discount_amount, tax_amount, total_amount, payment_method,
                payment_status, amount_paid, change_amount, prescription_id,
                prescription_number, prescriber_name, prescriber_license,
                cashier_id, status, sync_status, sync_version, updated_at, created_at)
             VALUES (?,?,?,?,?,'Rx Patient','30','0','0','30','cash','completed','30','0',?,
                'RX-1','Dr. Test','MED-1','cashier-1',?,'synced',1,
                '2026-10-04T00:00:00Z','2026-10-04T00:00:00Z')`
        )
        .run(`sale-${n}`, ORG, BRANCH, `S-${n}`, CUSTOMER, rxId, status);
}

beforeEach(async () => {
    await installRealDb();
    wipe();
});

async function search() {
    const { localRead } = await import("@/lib/localRead");
    const res = await localRead.searchPrescriptions({
        page: 1,
        page_size: 50,
        status_filter: null as never,
        include_expired: true,
        branch_id: BRANCH,
        organization_id: ORG,
    } as never);
    return res.items as Array<{ id: string; dispensed_count: number }>;
}

describe("dispensed_count parity, local path", () => {
    it("counts completed sales linked to the prescription", async () => {
        seedPrescription(RX, "RX-1");
        seedSale(RX, "completed", 1);
        seedSale(RX, "completed", 2);
        seedSale(RX, "completed", 3);
        const rows = await search();
        expect(rows.find((r) => r.id === RX)?.dispensed_count).toBe(3);
    });

    it("excludes non-completed sales, matching the server's status filter", async () => {
        seedPrescription(RX, "RX-1");
        seedSale(RX, "completed", 1);
        seedSale(RX, "cancelled", 2);
        seedSale(RX, "refunded", 3);
        seedSale(RX, "draft", 4);
        const rows = await search();
        expect(rows.find((r) => r.id === RX)?.dispensed_count).toBe(1);
    });

    it("does not count a sale that belongs to another prescription", async () => {
        seedPrescription(RX, "RX-1");
        seedPrescription(RX_UNUSED, "RX-2");
        seedSale(RX, "completed", 1);
        seedSale(RX_UNUSED, "completed", 2);
        seedSale(RX_UNUSED, "completed", 3);
        const rows = await search();
        expect(rows.find((r) => r.id === RX)?.dispensed_count).toBe(1);
        expect(rows.find((r) => r.id === RX_UNUSED)?.dispensed_count).toBe(2);
    });

    it("reports 0 rather than undefined for a prescription never dispensed", async () => {
        seedPrescription(RX_UNUSED, "RX-2");
        const rows = await search();
        expect(rows.find((r) => r.id === RX_UNUSED)?.dispensed_count).toBe(0);
    });

    it("ignores walk-in sales with no prescription", async () => {
        seedPrescription(RX, "RX-1");
        seedSale(null, "completed", 1);
        seedSale(null, "completed", 2);
        const rows = await search();
        expect(rows.find((r) => r.id === RX)?.dispensed_count).toBe(0);
    });

    it("keeps the count correct when a sale is counted for one row only", async () => {
        // Guards against an accidental cross join in the subquery.
        seedPrescription(RX, "RX-1");
        seedPrescription(RX_UNUSED, "RX-2");
        for (let i = 1; i <= 5; i++) seedSale(RX, "completed", 10 + i);
        seedSale(RX_UNUSED, "completed", 20);
        const rows = await search();
        expect(rows.find((r) => r.id === RX)?.dispensed_count).toBe(5);
        expect(rows.find((r) => r.id === RX_UNUSED)?.dispensed_count).toBe(1);
    });
});
