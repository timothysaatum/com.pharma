/**
 * P2, device half: a prescription can be created without a prescriber licence.
 *
 * The licence was required in four places, and each had to change:
 *   - the form validation in BOTH forms (PrescriptionsPage and
 *     PrescriptionSelector, which had its own copy of the check)
 *   - the envelope builder, which passed the raw value through
 *   - the created-projector, which defaulted a missing value to ""
 *   - the schema: NOT NULL DEFAULT ''
 *
 * The canonical absent form is SQL NULL, not "". Three distinct states for "no
 * licence" is how a `WHERE prescriber_license IS NOT NULL` filter quietly misses
 * rows, so the boundaries normalise.
 *
 * installRealDb() replays the real migration chain in memory. The device's own
 * laso.db is never opened.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["prescriptions", "event_outbox", "sales", "applied_events"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}

async function db() {
    return (await import("@/lib/localDb")).getDb();
}

async function licenceOf(id: string): Promise<string | null | undefined> {
    const rows = await (await db()).select<{ prescriber_license: string | null }[]>(
        "SELECT prescriber_license FROM prescriptions WHERE id = $1",
        [id]
    );
    return rows[0]?.prescriber_license;
}

function basePrescription(id: string, license: string | null) {
    return {
        id,
        organization_id: ORG,
        branch_id: BRANCH,
        prescription_number: `RX-${id.slice(0, 4)}`,
        customer_id: "cust-1",
        prescriber_name: "Dr. Test",
        prescriber_license: license,
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
        created_offline_at: null,
        synced_at: null,
        last_refill_date: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
    } as never;
}

beforeEach(async () => {
    await installRealDb();
    wipe();
});

describe("P2 device: creating without a licence", () => {
    it("the local schema accepts a NULL licence", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        // Would throw "NOT NULL constraint failed" before migrate_v36.
        await writeLocal.prescription(basePrescription("rx-null", null));
        expect(await licenceOf("rx-null")).toBeNull();
    });

    it("the envelope emits null, never an empty string", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(basePrescription("rx-env-null", null));
        const rows = await (await db()).select<{ event_type: string; payload: string }[]>(
            "SELECT event_type, payload FROM event_outbox WHERE aggregate_type = 'prescription'"
        );
        expect(rows).toHaveLength(1);
        const payload = JSON.parse(rows[0].payload);
        expect(payload.prescriber_license).toBeNull();
        expect("prescriber_license" in payload).toBe(true);
    });

    it("normalises an empty-string licence to null in the envelope", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(basePrescription("rx-env-empty", ""));
        const rows = await (await db()).select<{ payload: string }[]>(
            "SELECT payload FROM event_outbox WHERE aggregate_type = 'prescription'"
        );
        expect(JSON.parse(rows[0].payload).prescriber_license).toBeNull();
    });

    it("still carries a real licence through unchanged", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(basePrescription("rx-real", "MED-12345"));
        const rows = await (await db()).select<{ payload: string }[]>(
            "SELECT payload FROM event_outbox WHERE aggregate_type = 'prescription'"
        );
        expect(JSON.parse(rows[0].payload).prescriber_license).toBe("MED-12345");
        expect(await licenceOf("rx-real")).toBe("MED-12345");
    });

    it("the created-projector defaults a missing key to null, not ''", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        // An event from an older client that simply lacks the key.
        await applyEventLocally({
            event_id: "E1".padEnd(26, "0").toUpperCase(),
            aggregate_id: "rx-projected",
            aggregate_type: "prescription",
            event_type: "prescription_created",
            schema_version: 1,
            payload: {
                organization_id: ORG,
                branch_id: BRANCH,
                prescription_number: "RX-PROJ",
                customer_id: "cust-1",
                prescriber_name: "Dr. Test",
                issue_date: "2026-10-01",
                expiry_date: "2026-12-01",
                medications: [],
                refills_allowed: 1,
                refills_remaining: 1,
                status: "active",
            },
            dependencies: [],
            authored_at: new Date().toISOString(),
            authored_by: ORG,
            branch_id: BRANCH,
            org_id: ORG,
            hash_self: "h".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);
        expect(await licenceOf("rx-projected")).toBeNull();
    });

    it("the projector keeps a real licence from a server event", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally({
            event_id: "E2".padEnd(26, "0").toUpperCase(),
            aggregate_id: "rx-projected-2",
            aggregate_type: "prescription",
            event_type: "prescription_created",
            schema_version: 1,
            payload: {
                organization_id: ORG,
                branch_id: BRANCH,
                prescription_number: "RX-PROJ2",
                customer_id: "cust-1",
                prescriber_name: "Dr. Test",
                prescriber_license: "MED-SRV",
                issue_date: "2026-10-01",
                expiry_date: "2026-12-01",
                medications: [],
                refills_allowed: 1,
                refills_remaining: 1,
                status: "active",
            },
            dependencies: [],
            authored_at: new Date().toISOString(),
            authored_by: ORG,
            branch_id: BRANCH,
            org_id: ORG,
            hash_self: "h".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);
        expect(await licenceOf("rx-projected-2")).toBe("MED-SRV");
    });

    it("an update event may clear an existing licence to null", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(basePrescription("rx-clear", "MED-OLD"));
        expect(await licenceOf("rx-clear")).toBe("MED-OLD");

        await applyEventLocally({
            event_id: "E3".padEnd(26, "0").toUpperCase(),
            aggregate_id: "rx-clear",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            schema_version: 1,
            // The server's _UPDATABLE_FIELDS includes prescriber_license, and the
            // device UPDATABLE list does too.
            payload: { prescriber_license: null },
            dependencies: [],
            authored_at: new Date().toISOString(),
            authored_by: ORG,
            branch_id: BRANCH,
            org_id: ORG,
            hash_self: "h".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);
        expect(await licenceOf("rx-clear")).toBeNull();
    });

    it("the POS pre-flight can read a NULL licence without breaking", async () => {
        // POSPage.tsx copies prescriber_license onto the sale. It is already
        // nullable on sales, so a NULL must flow rather than crash.
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(basePrescription("rx-preflight", null));
        const rows = await (await db()).select<{ prescriber_license: string | null }[]>(
            `SELECT medications, refills_remaining, status, prescription_number,
                    prescriber_name, prescriber_license
               FROM prescriptions WHERE id = $1`,
            ["rx-preflight"]
        );
        expect(rows).toHaveLength(1);
        expect(rows[0].prescriber_license).toBeNull();
    });
});
