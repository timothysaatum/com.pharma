import { beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb } from "@/lib/__tests__/realDb";

async function userVersion(): Promise<number> {
    const db = await (await import("@/lib/localDb")).getDb();
    const rows = await db.select<{ user_version: number }[]>("PRAGMA user_version");
    return rows[0]?.user_version ?? 0;
}

async function columns(table: string): Promise<string[]> {
    const db = await (await import("@/lib/localDb")).getDb();
    const rows = await db.select<{ name: string }[]>(`PRAGMA table_info(${table})`);
    return rows.map((row) => row.name);
}

describe("MIGRATION v38", () => {
    beforeEach(async () => {
        await installRealDb();
    });

    it("creates the availability table and sale column, and is re-runnable", async () => {
        const { migrate_v38 } = await import("@/lib/localDb");
        const db = await (await import("@/lib/localDb")).getDb();

        await db.execute("DROP TABLE price_contract_availability");
        await db.execute("PRAGMA user_version = 37");

        await migrate_v38(db);

        expect(await userVersion()).toBe(38);
        expect(
            rawDb()
                .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?")
                .get("price_contract_availability")
        ).toBeTruthy();
        expect(await columns("sales")).toContain("contract_resolution");

        await migrate_v38(db);

        expect(await userVersion()).toBe(38);
        expect(await columns("sales")).toContain("contract_resolution");
    });
});
