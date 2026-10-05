/**
 * A locally-created prescription used to stay sync_status='pending' forever.
 *
 * THE CHAIN, all of it verified by reading the code before writing this test
 * ------------------------------------------------------------------------
 * 1. `writeLocal.prescription` upserts the row and `upsertLocal` FORCES
 *    sync_status='pending' (localWrite.ts:662), then appends the outbox event.
 * 2. Sync pushes it. The server accepts. `markOutboxResult(id, "accepted")`
 *    (localDb.ts:2305-2320) updates `event_outbox.status` and NOTHING ELSE. There
 *    is no code path anywhere that flips a read-model row's sync_status when its
 *    event is accepted.
 * 3. The device then pulls its own event back. `isLocallyAuthored`
 *    (localDb.ts:2380) finds the event_id still sitting in event_outbox, so
 *    syncEngine.ts:507-512 SKIPS the projector and only advances the cursor. So
 *    `_prescriptionCreated` — which hardcodes sync_status='synced' — never runs
 *    for the device's own event.
 * 4. Therefore the row stays 'pending' indefinitely.
 * 5. And `cachePrescriptions` skips any row whose sync_status is pending or
 *    conflict (localWrite.ts:949-952), so the server's authoritative row never
 *    overwrites it either.
 *
 * Observed on the owner's device (2026-10-05): RX-202610041042291 local `active 1/1,
 * sync_status 'pending', last_refill_date null` while the server holds
 * `filled 0/1, last_refill_date 2026-10-04`, with its prescription_created
 * accepted and sitting at event_log seq 97.
 *
 * Real installRealDb(), real writers, real queries. No mocking of the code under
 * test. The device's own laso.db is never opened.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const RX = "99999999-1111-2222-3333-444444444444";
const CUSTOMER = "cccccccc-1111-2222-3333-444444444444";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["prescriptions", "event_outbox", "customers", "sales", "applied_events"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}

async function db() {
    return (await import("@/lib/localDb")).getDb();
}

type Local = { status: string; refills_remaining: number; refills_allowed: number; sync_status: string; last_refill_date: string | null };

async function local(): Promise<Local> {
    const rows = await (await db()).select<Local[]>(
        `SELECT status, refills_remaining, refills_allowed, sync_status, last_refill_date
           FROM prescriptions WHERE id = $1`, [RX]);
    return rows[0];
}

async function outboxRows(): Promise<Array<{ event_id: string; status: string; aggregate_type: string; aggregate_id: string }>> {
    return (await db()).select(
        "SELECT event_id, status, aggregate_type, aggregate_id FROM event_outbox ORDER BY created_at");
}

function seedCustomer() {
    rawDb()
        .prepare(
            `INSERT INTO customers (id, organization_id, first_name, last_name, customer_type,
              is_active, sync_status, sync_version, created_at, updated_at)
             VALUES (?,?,'Kwame','Nkrumah','registered',1,'synced',1,'2026-10-04T10:00:00Z','2026-10-04T10:00:00Z')`
        )
        .run(CUSTOMER, ORG);
}

function offlinePrescription() {
    const now = "2026-10-04T10:44:26.000Z";
    return {
        id: RX,
        organization_id: ORG,
        branch_id: BRANCH,
        prescription_number: "RX-202610041042291",
        customer_id: CUSTOMER,
        prescriber_name: "Dr. Timothy Saatum",
        prescriber_license: "MED-1",
        prescriber_phone: null,
        prescriber_address: null,
        issue_date: "2026-10-04",
        expiry_date: "2026-11-03",
        medications: [],
        diagnosis: null,
        notes: null,
        special_instructions: null,
        refills_allowed: 1,
        refills_remaining: 1,
        status: "active",
        verified_by: null,
        verified_at: null,
        created_offline_at: now,
        synced_at: null,
        last_refill_date: null,
        created_at: now,
        updated_at: now,
    } as never;
}

/** What the server now holds after the online sale: filled 0/1. */
function serverRow() {
    return {
        id: RX,
        organization_id: ORG,
        branch_id: BRANCH,
        prescription_number: "RX-202610041042291",
        customer_id: CUSTOMER,
        prescriber_name: "Dr. Timothy Saatum",
        prescriber_license: "MED-1",
        prescriber_phone: null,
        prescriber_address: null,
        issue_date: "2026-10-04",
        expiry_date: "2026-11-03",
        medications: [],
        diagnosis: null,
        notes: null,
        special_instructions: null,
        refills_allowed: 1,
        refills_remaining: 0,
        last_refill_date: "2026-10-04",
        status: "filled",
        verified_by: null,
        verified_at: "2026-10-04T10:54:07.000Z",
        created_at: "2026-10-04T10:44:26.000Z",
        updated_at: "2026-10-04T10:54:07.000Z",
        sync_status: "synced",
        sync_version: 2,
    } as never;
}

beforeEach(async () => {
    await installRealDb();
    wipe();
    seedCustomer();
});

describe("a locally-created prescription reconciles once its event is accepted", () => {
    it("marks the row synced when the event is accepted", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        expect((await local())?.sync_status).toBe("pending");
        const [row] = await outboxRows();

        await markOutboxResult(row.event_id, "accepted");

        expect((await outboxRows())[0].status).toBe("accepted");
        // THE FIX: the read model now agrees with the outbox.
        expect((await local())?.sync_status).toBe("synced");
    });

    it("the pending count is 0 and the row is synced, so nothing looks stuck", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, getPendingOutboxCount } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [row] = await outboxRows();
        await markOutboxResult(row.event_id, "accepted");

        expect(await getPendingOutboxCount()).toBe(0);
        expect((await local())?.sync_status).toBe("synced");
    });

    // ── The interlocks. These are the constraints that matter most: a repair that
    // marks a row synced while an edit is still queued would let the next server
    // fetch silently discard that edit.
    it("INTERLOCK: stays pending while another event for it is still unsent", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, appendToOutbox } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [create] = await outboxRows();

        // A later local edit, still queued.
        await appendToOutbox({
            event_id: "EVT-UPDATE-1",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            aggregate_id: RX,
            org_id: ORG,
            branch_id: BRANCH,
            authored_by: ORG,
            authored_at: "2026-10-04T11:00:00.000Z",
            schema_version: 1,
            payload: { status: "cancelled" },
            dependencies: [],
            hash_self: "a".repeat(64),
            hash_prev: "b".repeat(64),
        } as never);

        await markOutboxResult(create.event_id, "accepted");

        // The create is on the server but the edit is not, so the row is NOT
        // reconciled. Marking it synced here would let the next cache overwrite
        // the pending cancel.
        expect((await local())?.sync_status).toBe("pending");
    });

    it("INTERLOCK: accepted_deferred counts as unsent", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, appendToOutbox, hasUnsentEventForAggregate } =
            await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [create] = await outboxRows();
        await appendToOutbox({
            event_id: "EVT-DEFERRED-1",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            aggregate_id: RX,
            org_id: ORG,
            branch_id: BRANCH,
            authored_by: ORG,
            authored_at: "2026-10-04T11:00:00.000Z",
            schema_version: 1,
            payload: { notes: "x" },
            dependencies: [],
            hash_self: "a".repeat(64),
            hash_prev: "b".repeat(64),
        } as never);
        await markOutboxResult("EVT-DEFERRED-1", "accepted_deferred");

        await markOutboxResult(create.event_id, "accepted");

        expect(await hasUnsentEventForAggregate("prescription", RX)).toBe(true);
        expect((await local())?.sync_status).toBe("pending");
    });

    it("a failed event also counts as unsent", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, hasUnsentEventForAggregate } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [create] = await outboxRows();
        await markOutboxResult(create.event_id, "failed", { code: "network_error", message: "x" });

        expect(await hasUnsentEventForAggregate("prescription", RX)).toBe(true);
        expect((await local())?.sync_status).toBe("pending");
    });

    it("rejected_permanent does NOT mark the row synced", async () => {
        // The server never received it, so 'synced' would be a lie. The row
        // stays pending — reported as a separate pre-existing gap, not papered
        // over here.
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [row] = await outboxRows();
        await markOutboxResult(row.event_id, "rejected_permanent", {
            code: "missing_prescriber_license",
            message: "x",
        });

        expect((await local())?.sync_status).toBe("pending");
    });

    it("an unrelated aggregate's accepted event does not touch this row", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, appendToOutbox } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [create] = await outboxRows();
        await appendToOutbox({
            event_id: "EVT-OTHER-AGG",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            aggregate_id: "some-other-prescription",
            org_id: ORG,
            branch_id: BRANCH,
            authored_by: ORG,
            authored_at: "2026-10-04T11:00:00.000Z",
            schema_version: 1,
            payload: {},
            dependencies: [],
            hash_self: "c".repeat(64),
            hash_prev: "d".repeat(64),
        } as never);
        await markOutboxResult("EVT-OTHER-AGG", "accepted");

        // The other aggregate reconciled; ours is untouched because its own event
        // is still pending.
        expect((await local())?.sync_status).toBe("pending");
        await markOutboxResult(create.event_id, "accepted");
        expect((await local())?.sync_status).toBe("synced");
    });
});

describe("cachePrescriptions no longer freezes a reconciled row", () => {
    it("applies the server's filled 0/1 to a row with nothing in flight", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [row] = await outboxRows();
        await markOutboxResult(row.event_id, "accepted");

        await writeLocal.cachePrescriptions([serverRow()]);

        const after = await local();
        expect(after.status).toBe("filled");
        expect(after.refills_remaining).toBe(0);
        expect(after.last_refill_date).toBe("2026-10-04");
    });

    it("INTERLOCK: never overwrites a row that still has an unsent event", async () => {
        const { writeLocal } = await import("@/lib/localWrite");

        // No accept: the local edit is still in flight, so the server's copy must
        // not win. This is the constraint that matters most.
        await writeLocal.prescription(offlinePrescription());
        await writeLocal.cachePrescriptions([serverRow()]);

        const after = await local();
        expect(after.status).toBe("active");
        expect(after.refills_remaining).toBe(1);
        expect(after.last_refill_date).toBeNull();
    });

    it("INTERLOCK: a still-pending EDIT blocks the overwrite even after the create was accepted", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult, appendToOutbox } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [create] = await outboxRows();
        await appendToOutbox({
            event_id: "EVT-EDIT-2",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            aggregate_id: RX,
            org_id: ORG,
            branch_id: BRANCH,
            authored_by: ORG,
            authored_at: "2026-10-04T11:00:00.000Z",
            schema_version: 1,
            payload: { status: "cancelled" },
            dependencies: [],
            hash_self: "a".repeat(64),
            hash_prev: "b".repeat(64),
        } as never);
        await markOutboxResult(create.event_id, "accepted");
        expect((await local())?.sync_status).toBe("pending");

        await writeLocal.cachePrescriptions([serverRow()]);

        // The pending edit survives. Local status stays 'active', not the
        // server's 'filled'.
        expect((await local())?.status).toBe("active");
        expect((await local())?.sync_status).toBe("pending");
    });

    it("still refuses to overwrite a row in 'conflict'", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { markOutboxResult } = await import("@/lib/localDb");

        await writeLocal.prescription(offlinePrescription());
        const [row] = await outboxRows();
        await markOutboxResult(row.event_id, "accepted");
        await (await db()).execute(
            "UPDATE prescriptions SET sync_status = 'conflict' WHERE id = $1", [RX]
        );

        await writeLocal.cachePrescriptions([serverRow()]);

        expect((await local())?.status).toBe("active");
    });

    it("caches normally when there is no local row at all", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.cachePrescriptions([serverRow()]);
        const after = await local();
        expect(after.status).toBe("filled");
        expect(after.sync_status).toBe("synced");
    });
});
