/**
 * Phase 3 gate 1 — CAN a branch_inventory quantity survive a replay?
 *
 * This is a characterisation test. It runs BEFORE any emitter is instrumented
 * and its job is to answer one question with numbers: when a device replays a
 * drug's `branch_inventory` event together with its `drug_batch` event(s), does
 * the final local `branch_inventory.quantity` equal the server's value, or does
 * it double?
 *
 * Why it matters
 * --------------
 * Today `branch_inventory.quantity` reaches the device ONLY through
 * `drug_batch_*` events, which bump it RELATIVELY (`quantity = quantity + n`).
 * Both the server's own `BranchInventoryProjector` and the device's
 * `buildBranchInventoryEnvelope` deliberately omit `quantity` — the server
 * comment is explicit: "We don't touch quantity, just metadata."
 *
 * Phase 3 has to close the gap where stock that did NOT arrive via a batch never
 * reaches the device. The obvious way is for the new stock event to carry an
 * ABSOLUTE quantity. That is exactly what this test is checking first: an
 * absolute-quantity event landing BEFORE the batch events would be added to
 * again by each of them.
 *
 * The truth in the fixture data
 * -----------------------------
 *   Gebedol     inventory 117, one batch of 117
 *   Amoxicilin  batches 147 + 100, inventory 247
 *
 * So the two drugs fail differently: Gebedol is one batch against one absolute
 * value (double → 234), Amoxicilin is two partial batches summing to the
 * absolute value (double → 494). A fix that only handles the single-batch case
 * would still break on the multi-batch one.
 *
 * Two payload variants are exercised, because that is the actual Phase 3 choice:
 *
 *   meta-only    what the server and the device emit TODAY (no `quantity` key)
 *   with-quantity  what an absolute-quantity stock event would carry
 *
 * Read the printed table, not the assertions. If `with-quantity` double-counts,
 * Phase 3 must not simply add a quantity to the payload — see the report.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
    TEST_BRANCH,
    insertBatch,
    insertInventory,
    installRealDb,
    rawDb,
    resetTables,
} from "./realDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

const ORG = "2d060ef8-a302-447c-91f4-b2fd30268341";

/** 8d4cc1a7-03c7-4a6a-8080-2bda5def026f — one batch of 117, inventory 117. */
const GEBEDOL = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";
const GEBEDOL_BATCH = "b0000000-0000-4000-8000-000000000001";

/** 9038e0e0-459f-4a6f-8bd1-72bb2b8447e0 — batches 147 + 100, inventory 247. */
const AMOXICILIN = "9038e0e0-459f-4a6f-8bd1-72bb2b8447e0";
const AMOX_BATCH_147 = "b0000000-0000-4000-8000-000000000002";
const AMOX_BATCH_100 = "b0000000-0000-4000-8000-000000000003";

interface Fixture {
    drugId: string;
    label: string;
    serverQty: number;
    batches: Array<{ id: string; qty: number }>;
}

const FIXTURES: Fixture[] = [
    {
        drugId: GEBEDOL,
        label: "Gebedol",
        serverQty: 117,
        batches: [{ id: GEBEDOL_BATCH, qty: 117 }],
    },
    {
        drugId: AMOXICILIN,
        label: "Amoxicilin",
        serverQty: 247,
        batches: [
            { id: AMOX_BATCH_147, qty: 147 },
            { id: AMOX_BATCH_100, qty: 100 },
        ],
    },
];

type PayloadVariant = "meta-only" | "with-quantity";
type Order = "inv-first" | "batch-first";
type DeviceState = "fresh" | "has-rows" | "partial";

let applyEventLocally: (e: EventEnvelope) => Promise<void>;

/** Result rows collected for the printed table. */
const rows: Array<{
    drug: string;
    variant: PayloadVariant;
    order: Order;
    device: DeviceState;
    serverQty: number;
    localQty: number;
    ok: boolean;
}> = [];

let seq = 0;

function envelope(
    eventType: string,
    aggregateType: string,
    aggregateId: string,
    payload: Record<string, unknown>
): EventEnvelope {
    seq += 1;
    return {
        event_id: `evt-${String(seq).padStart(4, "0")}`,
        org_id: ORG,
        seq,
        aggregate_id: aggregateId,
        aggregate_type: aggregateType,
        event_type: eventType,
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: "2026-01-01T00:00:00Z",
        authored_by: "bae475d9-994a-4d5b-abb2-32aa4b082602",
        branch_id: TEST_BRANCH,
        hash_self: "0".repeat(64),
        hash_prev: "0".repeat(64),
        received_at: "2026-01-01T00:00:00Z",
    } as EventEnvelope;
}

/**
 * The branch_inventory event, in both payload shapes.
 *
 * `meta-only` is byte-for-byte what the system produces today: the server
 * projector reads only `shelf_location` / `branch_selling_price` and never
 * touches quantity, and the device's `buildBranchInventoryEnvelope` omits it.
 */
function invEvent(fx: Fixture, variant: PayloadVariant): EventEnvelope {
    const payload: Record<string, unknown> = {
        branch_id: TEST_BRANCH,
        drug_id: fx.drugId,
        org_id: ORG,
        shelf_location: "Aisle 1",
        branch_selling_price: "12.00",
    };
    if (variant === "with-quantity") payload.quantity = fx.serverQty;
    return envelope("branch_inventory_created", "branch_inventory", `inv-${fx.drugId}`, payload);
}

/** One drug_batch_created event, as the device would receive it. */
function batchEvent(fx: Fixture, batch: { id: string; qty: number }): EventEnvelope {
    return envelope("drug_batch_created", "drug_batch", batch.id, {
        branch_id: TEST_BRANCH,
        drug_id: fx.drugId,
        batch_number: "B1",
        quantity: batch.qty,
        remaining_quantity: batch.qty,
        manufacturing_date: "2020-01-01",
        expiry_date: "2030-09-26",
    });
}

function localQty(drugId: string): number {
    const row = rawDb()
        .prepare("SELECT quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?")
        .get(TEST_BRANCH, drugId) as { quantity: number } | undefined;
    return row ? Number(row.quantity) : 0;
}

/**
 * A device that already holds some of the rows.
 *
 *   has-rows  correct inventory + every batch already present locally
 *   partial   correct inventory, but the LAST batch has not arrived yet
 *
 * `partial` is the ordinary case for a device that was offline when a delivery
 * was booked: the branch_inventory row has synced (it was there before), but a
 * new batch event has not. It is the scenario that decides whether an absolute
 * quantity in the payload is safe, because it is the one where a batch bump
 * lands on top of a freshly-set absolute value.
 */
function seedRows(fx: Fixture, state: DeviceState): void {
    insertInventory(rawDb(), {
        id: `inv-${fx.drugId}`,
        drugId: fx.drugId,
        quantity: fx.serverQty,
    });
    const batches = state === "partial" ? fx.batches.slice(0, -1) : fx.batches;
    for (const b of batches) {
        insertBatch(rawDb(), {
            id: b.id,
            drugId: fx.drugId,
            remaining: b.qty,
        });
    }
}

async function runCase(
    fx: Fixture,
    variant: PayloadVariant,
    order: Order,
    device: DeviceState
): Promise<number> {
    seq = 0;
    resetTables(["applied_events", "branch_inventory", "drug_batches"]);
    if (device !== "fresh") seedRows(fx, device);

    const inv = invEvent(fx, variant);
    const batches = fx.batches.map((b) => batchEvent(fx, b));

    if (order === "inv-first") {
        await applyEventLocally(inv);
        for (const b of batches) await applyEventLocally(b);
    } else {
        for (const b of batches) await applyEventLocally(b);
        await applyEventLocally(inv);
    }

    return localQty(fx.drugId);
}

describe("Phase 3 gate 1 — replaying inventory + batch events", () => {
    beforeAll(async () => {
        await installRealDb();
        ({ applyEventLocally } = await import("@/lib/localProjectors"));
    });

    beforeEach(() => {
        seq = 0;
    });

    afterAll(() => {
        const pad = (s: string, n: number) => s.padEnd(n);
        const padL = (s: string, n: number) => s.padStart(n);
        let out = "";
        out += "\n\n" + "=".repeat(96) + "\n";
        out += "PHASE 3 GATE 1 — local branch_inventory.quantity after replaying the event set\n";
        out += "=".repeat(96) + "\n";
        out +=
            pad("drug", 11) +
            pad("payload", 15) +
            pad("order", 12) +
            pad("device", 11) +
            padL("server", 8) +
            padL("local", 8) +
            "   verdict\n";
        out += "-".repeat(96) + "\n";
        for (const r of rows) {
            out +=
                pad(r.drug, 11) +
                pad(r.variant, 15) +
                pad(r.order, 12) +
                pad(r.device, 11) +
                padL(String(r.serverQty), 8) +
                padL(String(r.localQty), 8) +
                (r.ok ? "   OK" : `   DOUBLE-COUNTED (x${(r.localQty / r.serverQty).toFixed(1)})`) +
                "\n";
        }
        out += "-".repeat(96) + "\n";
        const bad = rows.filter((r) => !r.ok);
        out += bad.length
            ? `${bad.length} of ${rows.length} cases end at the WRONG quantity.\n`
            : `all ${rows.length} cases converge on the server value.\n`;
        out += "=".repeat(96) + "\n";
        // Printed to stderr so it survives regardless of reporter quiet level.
        console.error(out);
    });

    for (const fx of FIXTURES) {
        for (const variant of ["meta-only", "with-quantity"] as PayloadVariant[]) {
            for (const order of ["inv-first", "batch-first"] as Order[]) {
                for (const device of ["fresh", "has-rows", "partial"] as DeviceState[]) {
                    const label = `${fx.label}: ${variant} payload, ${order}, ${device} device`;

                    it(`${label} ends at ${fx.serverQty}`, async () => {
                        const local = await runCase(fx, variant, order, device);
                        // Record BEFORE asserting, so a failing case still
                        // reaches the table — the table is the deliverable.
                        rows.push({
                            drug: fx.label,
                            variant,
                            order,
                            device,
                            serverQty: fx.serverQty,
                            localQty: local,
                            ok: local === fx.serverQty,
                        });
                        // The assertion is the point of the exercise: it fails
                        // today for `with-quantity` / inv-first, and that failure
                        // is the evidence for the Phase 3 design decision.
                        expect(local).toBe(fx.serverQty);
                    });
                }
            }
        }
    }
});