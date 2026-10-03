/**
 * Live replay: does the REAL atlasdb log rebuild a device correctly?
 *
 * The fixture is exported from atlasdb itself (all 84 events of org
 * 2d060ef8…, plus the server's own branch_inventory and drug_batches rows), so
 * this is the actual production log, not a synthetic one.
 *
 * Two device states, because they fail differently:
 *
 *   fresh    nothing local. What a brand-new or wiped device does.
 *   partial  loaded with ONLY seq 1..69 — the 69 events the live device had
 *            already applied before this backfill. It then pulls 70..84 and must
 *            converge on exactly the same state, not accumulate or double.
 *
 * Checked per drug: branch_inventory.quantity, every lot (batch_number,
 * remaining_quantity, expiry_date), sellable_quantity, and the value
 * getSellableQuantity() returns — the last one because that is what the POS cart
 * reads, and a mismatch there is a "/0 available" bug.
 *
 * The offline catalogue is printed too, since that is the read that was broken
 * by reorder_quantity=0 and by branch-scoped search.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { installRealDb, rawDb, resetTables } from "./realDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

interface ServerBatch {
    id: string;
    batch_number: string;
    quantity: number;
    remaining_quantity: number;
    expiry_date: string | null;
}
interface ServerDrug {
    id: string;
    name: string;
    quantity: number;
    reserved_quantity: number;
    batches: ServerBatch[];
}
interface Contract {
    branch_id: string;
    server_head_seq: number;
    events: EventEnvelope[];
    drugs: ServerDrug[];
}

const CONTRACT: Contract = JSON.parse(
    readFileSync(join(__dirname, "fixtures", "stock-live-backfill-parity.json"), "utf8")
);

const PRE_BACKFILL_HEAD = 69;

let applyEventLocally: (e: EventEnvelope) => Promise<void>;
// localRead is an object literal of methods, not a set of module exports.
let localRead: typeof import("@/lib/localRead").localRead;

interface Line {
    device: string;
    drug: string;
    serverQty: number;
    localQty: number;
    serverLots: string;
    localLots: string;
    serverSellable: number;
    localSellable: number;
    getSellable: number;
    ok: boolean;
}
const lines: Line[] = [];

function seedDrugRow(id: string, name: string): void {
    const now = new Date().toISOString();
    rawDb()
        .prepare(
            `INSERT INTO drugs (id, name, sku, drug_type, reorder_quantity,
                                unit_price, reorder_level, is_active, is_deleted,
                                sync_status, sync_version, created_at, updated_at)
             VALUES (?,?,NULL,'otc',0,'0.00',0,1,0,'synced',1,?,?)`
        )
        .run(id, name, now, now);
}

function readInv(drugId: string, branchId: string) {
    const row = rawDb()
        .prepare(
            "SELECT quantity, sellable_quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?"
        )
        .get(branchId, drugId) as
        | { quantity: number; sellable_quantity: number }
        | undefined;
    return {
        quantity: row ? Number(row.quantity) : -1,
        sellable: row ? Number(row.sellable_quantity) : -1,
    };
}

function readLots(drugId: string, branchId: string): string[] {
    return (
        rawDb()
            .prepare(
                "SELECT batch_number, remaining_quantity, expiry_date FROM drug_batches WHERE branch_id = ? AND drug_id = ? ORDER BY batch_number"
            )
            .all(branchId, drugId) as Array<{
            batch_number: string;
            remaining_quantity: number;
            expiry_date: string | null;
        }>
    ).map((b) => `${b.batch_number}:${Number(b.remaining_quantity)}:${b.expiry_date ?? "-"}`);
}

/**
 * The server's sellable for a pair, computed with the same rule the device uses:
 * SUM of unexpired positive lots, minus leases held by other terminals. There
 * are no leases, so it is the lot sum.
 */
function serverSellable(d: ServerDrug): number {
    const today = new Date().toISOString().slice(0, 10);
    return d.batches
        .filter((b) => b.remaining_quantity > 0 && (b.expiry_date ?? "9999") >= today)
        .reduce((t, b) => t + b.remaining_quantity, 0);
}

async function runDevice(device: "fresh" | "partial"): Promise<void> {
    resetTables([
        "applied_events", "branch_inventory", "drug_batches",
        "drugs", "drug_categories", "stock_leases", "sales", "sync_meta",
    ]);

    if (device === "partial") {
        // Only the 69 events the live device had already applied.
        for (const d of CONTRACT.drugs) seedDrugRow(d.id, d.name);
        for (const ev of CONTRACT.events) {
            if (Number(ev.seq) > PRE_BACKFILL_HEAD) continue;
            await applyEventLocally({ ...ev, event_id: `${ev.event_id}-pre69` });
        }
    }

    for (const ev of CONTRACT.events) {
        if (device === "partial" && Number(ev.seq) <= PRE_BACKFILL_HEAD) continue;
        await applyEventLocally({ ...ev, event_id: `${ev.event_id}-${device}` });
    }

    for (const d of CONTRACT.drugs) {
        const inv = readInv(d.id, CONTRACT.branch_id);
        const localLots = readLots(d.id, CONTRACT.branch_id);
        const serverLots = d.batches.map(
            (b) => `${b.batch_number}:${b.remaining_quantity}:${b.expiry_date ?? "-"}`
        );
        const sSell = serverSellable(d);

        // getSellableQuantity is what the POS cart reads. It returns an object,
        // and notStocked when the pair has no inventory row at all.
        const gs = await localRead.getSellableQuantity(CONTRACT.branch_id, d.id);
        const getSell = gs.notStocked ? -1 : Number(gs.sellable);

        const lotsMatch =
            serverLots.length === localLots.length &&
            serverLots.every((v, i) => v === localLots[i]);
        const ok =
            inv.quantity === d.quantity &&
            inv.sellable === sSell &&
            getSell === sSell &&
            lotsMatch;

        lines.push({
            device,
            drug: d.name,
            serverQty: d.quantity,
            localQty: inv.quantity,
            serverLots: serverLots.join(" "),
            localLots: localLots.join(" "),
            serverSellable: sSell,
            localSellable: inv.sellable,
            getSellable: getSell,
            ok,
        });

        expect(inv.quantity, `${d.name} [${device}] branch_inventory.quantity`).toBe(d.quantity);
        expect(inv.sellable, `${d.name} [${device}] sellable_quantity`).toBe(sSell);
        expect(getSell, `${d.name} [${device}] getSellableQuantity`).toBe(sSell);
        expect(lotsMatch, `${d.name} [${device}] lots`).toBe(true);
    }
}

describe("live atlasdb log rebuilds a device", () => {
    beforeAll(async () => {
        await installRealDb();
        ({ applyEventLocally } = await import("@/lib/localProjectors"));
        ({ localRead } = await import("@/lib/localRead"));
    });

    beforeEach(() => {
        (globalThis as { localStorage?: Storage }).localStorage = {
            getItem: () => null,
            setItem: () => {},
            removeItem: () => {},
            clear: () => {},
            key: () => null,
            length: 0,
        } as unknown as Storage;
    });

    afterAll(() => {
        let out = "\nLIVE REPLAY — the real atlasdb log (84 events) on a real device\n";
        out +=
            "device | drug | server qty | local qty | lots | server sellable | " +
            "local sellable | getSellableQuantity | verdict\n";
        for (const l of lines) {
            out +=
                `${l.device} | ${l.drug} | ${l.serverQty} | ${l.localQty} | ` +
                `${l.localLots === l.serverLots ? "match" : "DIFFER"} | ` +
                `${l.serverSellable} | ${l.localSellable} | ${l.getSellable} | ` +
                `${l.ok ? "OK" : "MISMATCH"}\n`;
        }
        const bad = lines.filter((l) => !l.ok);
        out += bad.length
            ? `${bad.length} of ${lines.length} DISAGREE with atlasdb\n`
            : `all ${lines.length} drug/device combinations agree with atlasdb\n`;
        console.error(out);
    });

    it("fresh device", async () => {
        await runDevice("fresh");
    });

    it("partial device preloaded with seq 1..69", async () => {
        await runDevice("partial");
    });

    /**
     * The offline catalogue, with NO branch filter — the call that used to join
     * branch_inventory and show an empty formulary.
     *
     * Asserts every drug of the org is present. It deliberately does NOT assert
     * a count of 6: replaying this org's log faithfully reproduces SEVEN drugs,
     * because the log itself carries the E2E sentinel aggregate
     * 66666666-6666-6666-6666-666666666666 ("Paracetamol 500mg Tablets") at
     * seq 23..65. Those events pre-date this backfill (which created none) and
     * the drug does not exist in the server's drugs table, so the device shows
     * a phantom 7th row. Reported, not papered over.
     */
    it("the offline catalogue returns every drug of the org, with no branch filter", async () => {
        resetTables([
            "applied_events", "branch_inventory", "drug_batches",
            "drugs", "drug_categories", "stock_leases",
        ]);
        for (const ev of CONTRACT.events) {
            await applyEventLocally({ ...ev, event_id: `${ev.event_id}-cat` });
        }
        // No branch_id: the catalogue is organization-wide. This is the call that
        // used to filter by branch_inventory and show an empty formulary.
        const res = await localRead.searchDrugs({}, 1, 50);
        const rows = res.items as Array<{ name: string }>;
        const names = rows.map((r) => r.name).sort();
        console.error(
            `\nOFFLINE CATALOGUE (searchDrugs, no branch filter): ${rows.length} drugs -> ${JSON.stringify(names)}\n`
        );
        for (const d of CONTRACT.drugs) {
            expect(names, `${d.name} present in the offline catalogue`).toContain(d.name);
        }
        // The sentinel is in the log, so it lands on the device. Named here so
        // the count discrepancy is expected rather than surprising.
        const sentinel = names.filter((n) => n === "Paracetamol 500mg Tablets");
        console.error(
            `sentinel rows from the pre-existing log: ${sentinel.length} ` +
            `(expected 1 — it is in event_log, absent from the drugs table)\n`
        );
        expect(rows.length).toBeGreaterThanOrEqual(CONTRACT.drugs.length);
    });
});
