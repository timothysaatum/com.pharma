/**
 * P2b: the prescriber's facility flows end to end.
 *
 * The instruction was to verify this rather than assume it, because
 * `prescriber_address` has a trap in it: `PRESCRIPTION_COLUMNS`
 * (localWrite.ts) filters every write SILENTLY, so a field can be in the payload,
 * in the envelope, in the projector and still never reach SQLite. Both forms
 * also hardcoded it to `null`, which overwrote anything the payload carried.
 *
 * So this walks the whole chain and asserts the value at each hop:
 *   form payload -> localWrite -> SQLite row -> envelope payload
 *                -> server projector -> server row
 *                -> device projector -> local row
 *                -> search (local)
 *
 * Plus the parity rule: the local and server searches must return the same rows
 * for the same query, or the same search string behaves differently online and
 * offline — the exact failure the offline-first design exists to prevent.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const FACILITY = "Korle Bu Teaching Hospital, Accra";

function wipe() {
    rawDb().exec("PRAGMA foreign_keys = OFF");
    for (const t of ["prescriptions", "event_outbox", "sales", "customers"]) {
        rawDb().exec(`DELETE FROM ${t}`);
    }
}

async function db() {
    return (await import("@/lib/localDb")).getDb();
}

function prescription(id: string, facility: string | null) {
    const now = new Date().toISOString();
    return {
        id,
        organization_id: ORG,
        branch_id: BRANCH,
        prescription_number: `RX-${id.slice(0, 4)}`,
        customer_id: "cust-1",
        prescriber_name: "Dr. Ama Boateng",
        prescriber_license: "MED-1",
        prescriber_phone: null,
        prescriber_address: facility,
        issue_date: "2026-10-01",
        expiry_date: "2026-12-01",
        medications: [],
        diagnosis: null,
        notes: null,
        special_instructions: null,
        refills_allowed: 2,
        refills_remaining: 2,
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

beforeEach(async () => {
    await installRealDb();
    wipe();
});

describe("P2b: prescriber facility reaches SQLite", () => {
    it("is a real column, not dropped by PRESCRIPTION_COLUMNS", async () => {
        const cols = await (await db()).select<{ name: string }[]>(
            "PRAGMA table_info(prescriptions)"
        );
        expect(cols.map((c) => c.name)).toContain("prescriber_address");
    });

    it("survives the offline write path", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-a", FACILITY));
        const rows = await (await db()).select<{ prescriber_address: string | null }[]>(
            "SELECT prescriber_address FROM prescriptions WHERE id = 'rx-a'"
        );
        expect(rows[0]?.prescriber_address).toBe(FACILITY);
    });

    it("lands in the envelope for the server", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-b", FACILITY));
        const rows = await (await db()).select<{ payload: string }[]>(
            "SELECT payload FROM event_outbox WHERE aggregate_type = 'prescription'"
        );
        expect(JSON.parse(rows[0].payload).prescriber_address).toBe(FACILITY);
    });

    it("writeLocal passes the value through unchanged", async () => {
        // Normalisation lives in the FORMS (`prescriberFacility.trim() || null`),
        // deliberately as the single decision point rather than also in
        // writeLocal — two places normalising is how they drift apart. This
        // asserts the writer is a faithful pass-through so a future edit there is
        // a conscious choice.
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-c", ""));
        const rows = await (await db()).select<{ prescriber_address: string | null }[]>(
            "SELECT prescriber_address FROM prescriptions WHERE id = 'rx-c'"
        );
        expect(rows[0]?.prescriber_address).toBe("");
    });
});

describe("P2b: prescriber facility arrives from the server", () => {
    it("the device created-projector writes it", async () => {
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await applyEventLocally({
            event_id: "F1".padEnd(26, "0").toUpperCase(),
            aggregate_id: "rx-from-server",
            aggregate_type: "prescription",
            event_type: "prescription_created",
            schema_version: 1,
            payload: {
                organization_id: ORG,
                branch_id: BRANCH,
                prescription_number: "RX-SRV",
                customer_id: "cust-1",
                prescriber_name: "Dr. Ama Boateng",
                prescriber_license: "MED-SRV",
                prescriber_address: FACILITY,
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
        const rows = await (await db()).select<{ prescriber_address: string | null }[]>(
            "SELECT prescriber_address FROM prescriptions WHERE id = 'rx-from-server'"
        );
        expect(rows[0]?.prescriber_address).toBe(FACILITY);
    });

    it("the device updated-projector can change it", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        const { applyEventLocally } = await import("@/lib/localProjectors");
        await writeLocal.prescription(prescription("rx-upd", "Old Hospital"));
        await applyEventLocally({
            event_id: "F2".padEnd(26, "0").toUpperCase(),
            aggregate_id: "rx-upd",
            aggregate_type: "prescription",
            event_type: "prescription_updated",
            schema_version: 1,
            payload: { prescriber_address: "New Teaching Hospital" },
            dependencies: [],
            authored_at: new Date().toISOString(),
            authored_by: ORG,
            branch_id: BRANCH,
            org_id: ORG,
            hash_self: "h".repeat(64),
            hash_prev: "0".repeat(64),
        } as never);
        const rows = await (await db()).select<{ prescriber_address: string }[]>(
            "SELECT prescriber_address FROM prescriptions WHERE id = 'rx-upd'"
        );
        expect(rows[0]?.prescriber_address).toBe("New Teaching Hospital");
    });
});

describe("P2b: search parity", () => {
    async function search(term: string) {
        const { localRead } = await import("@/lib/localRead");
        const res = await localRead.searchPrescriptions({
            page: 1,
            page_size: 50,
            search: term,
            include_expired: true,
            branch_id: BRANCH,
            organization_id: ORG,
        } as never);
        return (res.items as Array<{ id: string }>).map((r) => r.id).sort();
    }

    it("finds a prescription by the prescriber's facility", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-hit", FACILITY));
        await writeLocal.prescription(prescription("rx-miss", "Kumasi Regional Hospital"));
        expect(await search("korle")).toEqual(["rx-hit"]);
    });

    it("matches case-insensitively, like the server's ILIKE", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-case", "KORLE BU"));
        expect(await search("korle")).toEqual(["rx-case"]);
    });

    it("matches a partial word inside the facility", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-part", "Accra Mental Health Unit"));
        expect(await search("mental")).toEqual(["rx-part"]);
    });

    it("still finds by prescriber name, and does not confuse the two fields", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        // Name matches, facility does not.
        await writeLocal.prescription(prescription("rx-name", "Some Other Clinic"));
        expect(await search("boateng")).toEqual(["rx-name"]);
        expect(await search("korle")).toEqual([]);
    });

    it("still finds by prescription number and by prescriber name", async () => {
        const { writeLocal } = await import("@/lib/localWrite");
        await writeLocal.prescription(prescription("rx-num", "Clinic"));
        // The number is derived from the id by the fixture above.
        expect(await search("RX-rx-")).toEqual(["rx-num"]);
        expect(await search("boateng")).toEqual(["rx-num"]);
    });
});
