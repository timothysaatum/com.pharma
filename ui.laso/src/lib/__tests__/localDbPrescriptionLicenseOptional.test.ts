/**
 * MIGRATION v36 — `prescriptions.prescriber_license` becomes nullable.
 *
 * SQLite cannot `DROP NOT NULL`, so the migration rebuilds the table. A rebuild
 * is the one kind of migration that can lose data, so this test starts from a
 * populated database and checks the row COUNT and every column VALUE afterwards,
 * including the two columns most likely to be dropped by accident:
 *
 *   - `is_deleted`, which exists in the v22 DDL but has no TypeScript field and
 *     no writer anywhere, so a hardcoded column list would silently drop it.
 *   - `medications`, which is JSON TEXT and must survive the copy byte for byte.
 *
 * Also pins NULL and '' as DISTINGUISHABLE values that both survive: the
 * migration deliberately does not normalise, because nothing consumes the
 * difference and rewriting history is riskier than reading both.
 *
 * installRealDb() replays the real migration chain, so this exercises the same
 * code path a device takes. The device's own laso.db is never opened.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";
const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";

type Col = { name: string; notnull: number; dflt_value: string | null };

async function cols(): Promise<Col[]> {
    const db = await (await import("@/lib/localDb")).getDb();
    return db.select<Col[]>("PRAGMA table_info(prescriptions)");
}

function insert(id: string, license: string | null, isDeleted = 0) {
    rawDb()
        .prepare(
            `INSERT INTO prescriptions
               (id, organization_id, branch_id, prescription_number, customer_id,
                prescriber_name, prescriber_license, issue_date, expiry_date,
                medications, refills_allowed, refills_remaining, status,
                is_deleted, sync_status, sync_version, updated_at, created_at)
             VALUES (?,?,?,?,?,'Dr. Test',?,'2026-10-01','2026-12-01',
                '[{"drug_id":"d1","drug_name":"Gebedol","dosage":"5mg","frequency":"bd","duration":"7d","quantity":10}]',
                3,2,'active',?,'synced',1,'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')`
        )
        .run(
            id, ORG, BRANCH, `RX-${id.slice(0, 4)}`, "cust-1", license, isDeleted
        );
}

async function userVersion(): Promise<number> {
    const db = await (await import("@/lib/localDb")).getDb();
    // PRAGMA user_version names its column after the pragma, not "v".
    const rows = await db.select<{ user_version: number }[]>("PRAGMA user_version");
    return rows[0]?.user_version ?? 0;
}

describe("MIGRATION v36", () => {
    beforeEach(async () => {
        await installRealDb();
    });

    it("runs the chain up to the current head", async () => {
        // Bumped by migration v37 (cross-org customer cleanup). This asserts the
        // CHAIN reached its head, not that it stopped at a particular number.
        expect(await userVersion()).toBe(37);
    });

    it("leaves prescriber_license nullable", async () => {
        const c = (await cols()).find((x) => x.name === "prescriber_license");
        expect(c).toBeTruthy();
        expect(c?.notnull).toBe(0);
    });

    it("preserves rows and every column value across the rebuild", async () => {
        // A fresh installRealDb has already run v36, so rebuild the pre-v36 state
        // to prove the migration is lossless rather than only proving the
        // end-state column type.
        const { migrate_v36 } = await import("@/lib/localDb");
        const db = await (await import("@/lib/localDb")).getDb();

        // Put the table back into the v35 shape: NOT NULL licence.
        await db.execute("DROP TABLE prescriptions");
        await db.execute(`
          CREATE TABLE prescriptions (
            id TEXT NOT NULL PRIMARY KEY, organization_id TEXT NOT NULL DEFAULT '',
            branch_id TEXT NOT NULL DEFAULT '', prescription_number TEXT NOT NULL DEFAULT '',
            customer_id TEXT NOT NULL DEFAULT '', prescriber_name TEXT NOT NULL DEFAULT '',
            prescriber_license TEXT NOT NULL DEFAULT '', prescriber_phone TEXT,
            prescriber_address TEXT, issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT,
            diagnosis TEXT, notes TEXT, special_instructions TEXT,
            medications TEXT NOT NULL DEFAULT '[]', refills_allowed INTEGER NOT NULL DEFAULT 0,
            refills_remaining INTEGER NOT NULL DEFAULT 0, last_refill_date TEXT,
            status TEXT NOT NULL DEFAULT 'active', verified_by TEXT, verified_at TEXT,
            created_offline_at TEXT, is_deleted INTEGER NOT NULL DEFAULT 0,
            sync_status TEXT NOT NULL DEFAULT 'synced', sync_version INTEGER NOT NULL DEFAULT 1,
            synced_at TEXT, updated_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')
        `);
        await db.execute("PRAGMA user_version = 35");

        insert("rx-with-licence", "MED-12345");
        insert("rx-empty-licence", "");
        insert("rx-no-facility", "MED-1", 1);

        const before = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM prescriptions ORDER BY id"
        );
        expect(before).toHaveLength(3);

        await migrate_v36(db);

        const after = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM prescriptions ORDER BY id"
        );
        expect(after).toHaveLength(3);

        // Every value identical, including medications JSON and is_deleted.
        expect(after).toEqual(before);

        // And the licence column is now nullable.
        const c = (await cols()).find((x) => x.name === "prescriber_license");
        expect(c?.notnull).toBe(0);
        expect(await userVersion()).toBe(36);
    });

    it("is idempotent — running it twice changes nothing", async () => {
        const { migrate_v36 } = await import("@/lib/localDb");
        const db = await (await import("@/lib/localDb")).getDb();
        insert("rx-idem", "");
        const first = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM prescriptions ORDER BY id"
        );
        await migrate_v36(db);
        await migrate_v36(db);
        const second = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM prescriptions ORDER BY id"
        );
        expect(second).toEqual(first);
    });

    it("actually accepts a NULL licence after the migration", async () => {
        insert("rx-null", null);
        const db = await (await import("@/lib/localDb")).getDb();
        const rows = await db.select<{ prescriber_license: string | null }[]>(
            "SELECT prescriber_license FROM prescriptions WHERE id = 'rx-null'"
        );
        expect(rows[0]?.prescriber_license).toBeNull();
    });
});
