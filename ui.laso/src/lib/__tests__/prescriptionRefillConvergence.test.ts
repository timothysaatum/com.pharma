/**
 * P1, device half: the local prescriptions row must converge with the server.
 *
 * THE DEFECT
 * ----------
 * `prescription_refill_used` had a projector here and no emitter anywhere, and
 * `_saleCreated` never touched `prescriptions`. So after an online sale the
 * device row still read `active 3/3` while the sale row was linked — and the POS
 * pre-flight (POSPage.tsx:342-345) gates checkout on exactly that stale row.
 *
 * The second half of the defect is subtler and is the reason for the
 * `applied_events` marker: a device that made the sale OFFLINE already
 * decremented locally. When the server began echoing the refill for that sale,
 * the device would have decremented a second time unless it could recognise the
 * echo as its own.
 *
 * Real `installRealDb()` (replays the production localDb migration chain into
 * in-memory SQLite) and the real projectors plus the real offline manager. The
 * device's own laso.db is never opened.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const RX = "dddddddd-1111-2222-3333-444444444444";
const RX_DRUG = "bbbbbbbb-1111-2222-3333-444444444444";

const TABLES = [
    "prescriptions",
    "sales",
    "event_outbox",
    "applied_events",
    "offline_sales",
    "audit_logs",
    "drug_batches",
    "branch_inventory",
];

type RxRow = {
    status: string;
    refills_allowed: number;
    refills_remaining: number;
    last_refill_date: string | null;
    verified_by: string | null;
    verified_at: string | null;
    sync_status: string;
};

let seq = 0;

async function db() {
    return (await import("@/lib/localDb")).getDb();
}

async function rx(id = RX): Promise<RxRow | undefined> {
    const rows = await (await db()).select<RxRow[]>(
        `SELECT status, refills_allowed, refills_remaining, last_refill_date,
                verified_by, verified_at, sync_status
           FROM prescriptions WHERE id = $1`,
        [id]
    );
    return rows[0];
}

async function saleCount(rxId = RX): Promise<number> {
    const rows = await (await db()).select<{ n: number }[]>(
        "SELECT COUNT(*) AS n FROM sales WHERE prescription_id = $1",
        [rxId]
    );
    return rows[0]?.n ?? 0;
}

async function appliedEventIds(): Promise<string[]> {
    const rows = await (await db()).select<{ event_id: string }[]>(
        "SELECT event_id FROM applied_events WHERE event_type = 'prescription_refill_used'"
    );
    return rows.map((r) => r.event_id);
}

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of TABLES) rawDb().exec(`DELETE FROM ${t}`);
}

function stock() {
    const h = rawDb();
    h.prepare(
        `INSERT INTO branch_inventory
           (id, branch_id, drug_id, quantity, reserved_quantity, sellable_quantity,
            location, selling_price, sync_status, sync_version, synced_at, updated_at, created_at)
         VALUES (?,?,?,?,?,?,NULL,NULL,'synced',1,NULL,?,?)`
    ).run("inv-1", BRANCH, RX_DRUG, 500, 0, 500, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z");
    h.prepare(
        `INSERT INTO drug_batches
           (id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
            manufacturing_date, expiry_date, sync_status, sync_version,
            synced_at, updated_at, created_at)
         VALUES (?,?,?,'B1',?,?,'2020-01-01','2030-01-01','synced',1,NULL,?,?)`
    ).run("batch-1", BRANCH, RX_DRUG, 500, 500, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z");
}

function envelope(
    eventType: string,
    aggregateType: string,
    aggregateId: string,
    payload: Record<string, unknown>,
    eventId?: string
) {
    seq += 1;
    return {
        event_id: eventId ?? `evt-${eventType}-${seq}`,
        aggregate_id: aggregateId,
        aggregate_type: aggregateType,
        event_type: eventType,
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: "2026-10-04T09:00:00.000Z",
        authored_by: ORG,
        branch_id: BRANCH,
        org_id: ORG,
        hash_self: "h".repeat(64),
        hash_prev: "0".repeat(64),
    } as never;
}

function rxCreated(over: Record<string, unknown> = {}) {
    return envelope(
        "prescription_created",
        "prescription",
        RX,
        {
            organization_id: ORG,
            branch_id: BRANCH,
            prescription_number: "RX-SRV-1",
            customer_id: "cust-1",
            prescriber_name: "Dr. Test",
            prescriber_license: "MED-1",
            issue_date: "2026-10-01",
            expiry_date: "2026-12-01",
            medications: [
                {
                    drug_id: RX_DRUG,
                    drug_name: "Gebedol",
                    dosage: "5mg",
                    frequency: "bd",
                    duration: "7d",
                    quantity: 10,
                },
            ],
            refills_allowed: 3,
            refills_remaining: 3,
            status: "active",
            ...over,
        }
    );
}

/** Shaped exactly like the payload the server's SaleProjector emits. */
function serverRefillPayload(over: Record<string, unknown> = {}) {
    return {
        prescription_id: RX,
        sale_id: "eeeeeeee-1111-2222-3333-444444444444",
        sale_number: "OFF-1",
        organization_id: ORG,
        branch_id: BRANCH,
        refills_remaining: 2,
        refills_allowed: 3,
        status: "active",
        last_refill_date: "2026-10-04",
        verified_by: null,
        verified_at: "2026-10-04T09:00:00+00:00",
        source: "offline_sale_sync",
        over_dispensed: false,
        status_before: "active",
        refills_before: 3,
        ...over,
    };
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    stock();
});

describe("P1 device: S-A, an online sale elsewhere reaches this device", () => {
    it("applying the server's refill event updates the counter", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated());
        expect((await rx())?.refills_remaining).toBe(3);

        await applyEventLocally(
            envelope(
                "prescription_refill_used",
                "prescription",
                RX,
                serverRefillPayload(),
                "FA5BB38695815707FDFE0DB5EA"
            )
        );

        const row = await rx();
        // REGRESSION: was `refills_remaining = 3`, status active, no last_refill_date.
        expect(row?.refills_remaining).toBe(2);
        expect(row?.status).toBe("active");
        expect(row?.last_refill_date).toBe("2026-10-04");
        expect(row?.verified_at).toBe("2026-10-04T09:00:00+00:00");
    });

    it("fills the prescription when the server says zero", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated({ refills_allowed: 1, refills_remaining: 1 }));
        await applyEventLocally(
            envelope("prescription_refill_used", "prescription", RX,
                serverRefillPayload({
                    refills_remaining: 0, refills_before: 1,
                    refills_allowed: 1, status: "filled",
                }), "E1")
        );
        const row = await rx();
        expect(row?.refills_remaining).toBe(0);
        expect(row?.status).toBe("filled");
    });

    it("sets 'filled' at zero when the server sent no status", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated({ refills_allowed: 1, refills_remaining: 1 }));
        // The key must be ABSENT, not "active": COALESCE only falls back when the
        // server genuinely said nothing about the status.
        const noStatus = serverRefillPayload({ refills_remaining: 0, refills_allowed: 1 });
        delete (noStatus as Record<string, unknown>).status;
        await applyEventLocally(
            envelope("prescription_refill_used", "prescription", RX, noStatus, "E2")
        );
        const row = await rx();
        expect(row?.refills_remaining).toBe(0);
        expect(row?.status).toBe("filled");
    });

    it("falls back to a relative decrement for an event with no post-state", async () => {
        // Events already sitting in a device outbox carry none of the new keys,
        // because this event type never had an emitter before this change.
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated());
        await applyEventLocally(
            envelope("prescription_refill_used", "prescription", RX,
                { refill_date: "2026-10-04" }, "LEGACY")
        );
        const row = await rx();
        expect(row?.refills_remaining).toBe(2);
        expect(row?.last_refill_date).toBe("2026-10-04");
    });

    it("does not resurrect a prescription this device cancelled", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated());
        await applyEventLocally(
            envelope("prescription_cancelled", "prescription", RX, {}, "CANCEL1")
        );
        expect((await rx())?.status).toBe("cancelled");

        // An event with no status must leave 'cancelled' alone. The old SQL set
        // status = 'active' unconditionally on every refill.
        await applyEventLocally(
            envelope("prescription_refill_used", "prescription", RX,
                { refill_date: "2026-10-04" }, "LEGACY2")
        );
        expect((await rx())?.status).toBe("cancelled");
    });
});

describe("P1 device: replay and self-echo", () => {
    it("replaying the same event changes nothing", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally(rxCreated());
        const e = envelope("prescription_refill_used", "prescription", RX,
            serverRefillPayload(), "REPLAY1");
        await applyEventLocally(e);
        expect((await rx())?.refills_remaining).toBe(2);
        for (let i = 0; i < 3; i++) await applyEventLocally(e);
        expect((await rx())?.refills_remaining).toBe(2);
    });

    it("the server's echo of THIS device's offline sale does not decrement twice", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { offlineSalesManager } = await import("@/lib/offlineSalesManager");
        const { prescriptionRefillUsedEventId } = await import("@/lib/refillEventId");

        await applyEventLocally(rxCreated());

        const saleId = "eeeeeeee-1111-2222-3333-444444444444";
        const res = await offlineSalesManager.recordSaleTransaction(
            {
                id: saleId,
                organization_id: ORG,
                branch_id: BRANCH,
                sale_number: "OFF-1",
                customer_id: "cust-1",
                cashier_id: "cashier-1",
                pharmacist_id: null,
                payment_method: "cash",
                payment_status: "completed",
                total_amount: 30,
                subtotal: 30,
                discount_amount: 0,
                tax_amount: 0,
                amount_paid: 30,
                change_amount: 0,
                prescription_id: RX,
                status: "completed",
            } as never,
            [
                {
                    drug_id: RX_DRUG,
                    drug_name: "Gebedol",
                    drug_sku: "SKU",
                    quantity: 3,
                    unit_price: 10,
                    discount_amount: 0,
                    subtotal: 30,
                    total_price: 30,
                    batch_id: null,
                    requires_prescription: true,
                    prescription_verified: true,
                },
            ] as never,
            [{ drug_id: RX_DRUG, delta: -3 }],
            "IDEM-1"
        );
        expect((res as { success: boolean }).success).toBe(true);

        // The device decremented once, locally.
        expect((await rx())?.refills_remaining).toBe(2);
        expect((await rx())?.last_refill_date).not.toBeNull();

        // And it recorded the id the server will derive for that same dispense.
        const expectedId = await prescriptionRefillUsedEventId(RX, saleId);
        expect(await appliedEventIds()).toContain(expectedId);

        // Now the server echoes it. Without the marker this would be 2 -> 1.
        await applyEventLocally(
            envelope(
                "prescription_refill_used",
                "prescription",
                RX,
                serverRefillPayload({ sale_id: saleId, refills_remaining: 2 }),
                expectedId
            )
        );
        expect((await rx())?.refills_remaining).toBe(2);
    });

    it("does not flip a locally-created prescription to 'synced'", async () => {
        // The old UPDATE hardcoded `sync_status = 'synced'`, so dispensing a
        // prescription that was created offline on this device declared its
        // unsynced record already reconciled.
        //
        // Note the UPDATE no longer writes sync_status AT ALL, rather than
        // writing 'pending'. 'pending' would be worse: cachePrescriptions skips
        // pending rows (localWrite.ts:950), so the server could then never
        // overwrite this row from a list fetch. The flag answers "does this
        // prescription RECORD have unsynced local edits"; a consumed refill is
        // announced through sale_created, not through this row.
        const { writeLocal } = await import("@/lib/localWrite");
        const { offlineSalesManager } = await import("@/lib/offlineSalesManager");
        const nowIso = new Date().toISOString();
        await writeLocal.prescription({
            id: RX,
            organization_id: ORG,
            branch_id: BRANCH,
            prescription_number: "RX-LOCAL-1",
            customer_id: "cust-1",
            prescriber_name: "Dr. Test",
            prescriber_license: "MED-1",
            prescriber_phone: null,
            prescriber_address: null,
            issue_date: "2026-10-01",
            expiry_date: "2026-12-01",
            medications: [],
            diagnosis: null,
            notes: null,
            special_instructions: null,
            refills_allowed: 3,
            refills_remaining: 3,
            status: "active",
            verified_by: null,
            verified_at: null,
            created_offline_at: nowIso,
            synced_at: null,
            last_refill_date: null,
            created_at: nowIso,
            updated_at: nowIso,
        } as never);
        expect((await rx())?.sync_status).toBe("pending");

        await offlineSalesManager.recordSaleTransaction(
            {
                id: "sale-sync-status",
                organization_id: ORG,
                branch_id: BRANCH,
                sale_number: "OFF-2",
                customer_id: "cust-1",
                cashier_id: "cashier-1",
                payment_method: "cash",
                payment_status: "completed",
                total_amount: 30,
                subtotal: 30,
                discount_amount: 0,
                tax_amount: 0,
                amount_paid: 30,
                change_amount: 0,
                prescription_id: RX,
                status: "completed",
            } as never,
            [
                {
                    drug_id: RX_DRUG, drug_name: "G", drug_sku: "S", quantity: 3,
                    unit_price: 10, discount_amount: 0, subtotal: 30, total_price: 30,
                    batch_id: null, requires_prescription: true, prescription_verified: true,
                },
            ] as never,
            [{ drug_id: RX_DRUG, delta: -3 }],
            "IDEM-2"
        );
        // The row was pulled from the server, so it was 'synced'; the offline
        // decrement must not leave it claiming to still be.
        const row = await rx();
        expect(row?.sync_status).not.toBe("synced");
    });

    it("does not write the marker when the sale had no prescription", async () => {
        const { offlineSalesManager } = await import("@/lib/offlineSalesManager");
        await offlineSalesManager.recordSaleTransaction(
            {
                id: "sale-no-rx",
                organization_id: ORG,
                branch_id: BRANCH,
                sale_number: "OFF-3",
                customer_id: "cust-1",
                cashier_id: "cashier-1",
                payment_method: "cash",
                payment_status: "completed",
                total_amount: 30,
                subtotal: 30,
                discount_amount: 0,
                tax_amount: 0,
                amount_paid: 30,
                change_amount: 0,
                prescription_id: null,
                status: "completed",
            } as never,
            [
                {
                    drug_id: RX_DRUG, drug_name: "G", drug_sku: "S", quantity: 3,
                    unit_price: 10, discount_amount: 0, subtotal: 30, total_price: 30,
                    batch_id: null, requires_prescription: false, prescription_verified: false,
                },
            ] as never,
            [{ drug_id: RX_DRUG, delta: -3 }],
            "IDEM-3"
        );
        expect(await appliedEventIds()).toEqual([]);
    });
});

describe("P1 device: the offline transaction still records the sale", () => {
    it("keeps the sale row linked and the counter decremented once", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { offlineSalesManager } = await import("@/lib/offlineSalesManager");
        await applyEventLocally(rxCreated());
        await offlineSalesManager.recordSaleTransaction(
            {
                id: "sale-link",
                organization_id: ORG,
                branch_id: BRANCH,
                sale_number: "OFF-4",
                customer_id: "cust-1",
                cashier_id: "cashier-1",
                payment_method: "cash",
                payment_status: "completed",
                total_amount: 30,
                subtotal: 30,
                discount_amount: 0,
                tax_amount: 0,
                amount_paid: 30,
                change_amount: 0,
                prescription_id: RX,
                status: "completed",
            } as never,
            [
                {
                    drug_id: RX_DRUG, drug_name: "G", drug_sku: "S", quantity: 3,
                    unit_price: 10, discount_amount: 0, subtotal: 30, total_price: 30,
                    batch_id: null, requires_prescription: true, prescription_verified: true,
                },
            ] as never,
            [{ drug_id: RX_DRUG, delta: -3 }],
            "IDEM-4"
        );
        expect(await saleCount()).toBe(1);
        expect((await rx())?.refills_remaining).toBe(2);
    });
});
