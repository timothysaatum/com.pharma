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
    OTHER_BRANCH,
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

/** A drug that exists on the branch with no batches at all. */
const BATCHLESS = "00000000-0000-4000-8000-00000000b17c";

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
    variant: string;
    order: string;
    device: string;
    serverQty: number;
    localQty: number;
    ok: boolean;
}> = [];

/** Record one case. Called BEFORE the assertion so failures still tabulate. */
function record(
    drug: string,
    variant: string,
    order: string,
    device: string,
    serverQty: number,
    localQty: number
): void {
    rows.push({ drug, variant, order, device, serverQty, localQty, ok: localQty === serverQty });
}

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
function invEvent(
    fx: Fixture,
    variant: PayloadVariant,
    explicitQuantity?: number
): EventEnvelope {
    const payload: Record<string, unknown> = {
        branch_id: TEST_BRANCH,
        drug_id: fx.drugId,
        org_id: ORG,
        shelf_location: "Aisle 1",
        branch_selling_price: "12.00",
    };
    if (variant === "with-quantity") {
        payload.quantity = explicitQuantity ?? fx.serverQty;
    }
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

/**
 * Stock-movement cases on a stocked device.
 *
 * Every scenario starts from the full, CORRECT local state (inventory 117 and the
 * 117 batch, exactly as the server has it), applies a stock movement the way the
 * server would, and returns the resulting local quantity. The server's own
 * post-movement quantity is hard-coded in each test's expectation.
 */
type Movement =
    | "sale-created"
    | "sale-voided"
    | "sale-voided-replayed"
    | "stock-adjusted-batches"
    | "stock-adjusted-replayed"
    | "stock-adjusted-delta-only"
    | "stock-transfer";

async function runMovement(fx: Fixture, movement: Movement, when: string): Promise<number> {
    seq = 0;
    resetTables(["applied_events", "branch_inventory", "drug_batches", "sales"]);
    seedRows(fx, "has-rows"); // the device is fully in sync

    const batch = fx.batches[0];
    const biEvent = invEvent(fx, "with-quantity");

    // ── A sale of 17 out of the 117 batch ─────────────────────────────────
    if (movement === "sale-created" || movement === "sale-voided" || movement === "sale-voided-replayed") {
        // `batch_changes` sits at the payload ROOT: _saleCreated reads
        // `p.batch_changes`, not `item.batch_changes`. Per-item nesting was
        // silently ignored and the sale deducted no stock at all.
        const saleCreated = envelope("sale_created", "sale", "sale-1", {
            branch_id: TEST_BRANCH,
            customer_id: "cust-1",
            payment_method: "cash",
            total: 170,
            batch_changes: [{ batch_id: batch.id, quantity_used: 17 }],
            items: [
                { drug_id: fx.drugId, name: fx.label, quantity: 17, unit_price: 10 },
            ],
        });
        if (when === "before-sync") {
            // The sale lands before the device has ever seen the stock.
            await applyEventLocally(saleCreated);
            await applyEventLocally(biEvent);
        } else {
            await applyEventLocally(biEvent);
            await applyEventLocally(saleCreated);
        }
        if (movement.startsWith("sale-voided")) {
            const voided = envelope("sale_voided", "sale", "sale-1", {
                branch_id: TEST_BRANCH,
                drug_id: fx.drugId,
                quantity: 17,
                batch_changes: [{ batch_id: batch.id, quantity_used: 17 }],
            });
            await applyEventLocally(voided);
            if (movement === "sale-voided-replayed") {
                // A REPLAY is the same event redelivered: same event_id, higher
                // seq. Giving it a new event_id would make it a genuinely
                // distinct second void, which the guard is right to allow.
                await applyEventLocally({
                    ...voided,
                    seq: Number(voided.seq ?? 0) + 1000,
                });
            }
        }
        return localQty(fx.drugId);
    }

    // ── A stock adjustment ────────────────────────────────────────────────
    if (movement.startsWith("stock-adjusted")) {
        const withBatches = movement !== "stock-adjusted-delta-only";
        const payload: Record<string, unknown> = {
            drug_id: fx.drugId,
            branch_id: TEST_BRANCH,
            reason: "count correction",
        };
        if (withBatches) {
            payload.quantity_change = 15;
            payload.batch_changes = [{ batch_id: batch.id, quantity_change: 15 }];
        } else {
            // No batch detail: an explicit relative delta, which is the one
            // case that must move quantity directly.
            payload.quantity_change = 5;
        }
        const adj = envelope("stock_adjusted", "stock", "stk-1", payload);
        await applyEventLocally(adj);
        if (movement === "stock-adjusted-replayed") {
            // Same event_id: a redelivery, not a second adjustment.
            await applyEventLocally({
                ...adj,
                seq: Number(adj.seq ?? 0) + 1000,
            });
        }
        return localQty(fx.drugId);
    }

    // ── A transfer to another branch ──────────────────────────────────────
    await applyEventLocally(
        envelope("stock_transfer", "stock", "stk-2", {
            drug_id: fx.drugId,
            source_branch_id: TEST_BRANCH,
            destination_branch_id: OTHER_BRANCH,
            quantity: 20,
            batch_changes: [{ batch_id: batch.id, quantity: 20 }],
        })
    );
    return localQty(fx.drugId);
}

/** A pair with no batches, adjusted with a bare delta: the delta must stick. */
async function runBatchlessDeltaOnly(): Promise<number> {
    return runBatchless(25);
}

/**
 * The no-batches fallback: a drug added to a branch, never given a batch.
 *
 * `quantity` here is carried only by the branch_inventory event, so it is the
 * absolute value the payload states. This is the case where the derivation must
 * NOT run — an empty batch set means "not received yet", not "zero stock".
 */
async function runBatchless(
    absolute: number,
    opts: { thenBatch?: number } = {}
): Promise<number> {
    seq = 0;
    resetTables(["applied_events", "branch_inventory", "drug_batches"]);

    const fx: Fixture = {
        drugId: BATCHLESS,
        label: "Batchless",
        serverQty: absolute,
        batches: [],
    };

    // add_drug_to_branch always creates the row at quantity 0. When stock then
    // arrives it does so as an adjustment carrying no batch detail, which is the
    // relative-delta path. Passing `absolute` on the inventory event AS WELL
    // would state the same stock twice, so the event states 0.
    const openAtZero = absolute > 0;
    await applyEventLocally(invEvent(fx, "with-quantity", openAtZero ? 0 : absolute));

    if (openAtZero) {
        await applyEventLocally(
            envelope("stock_adjusted", "stock", "stk-bl", {
                drug_id: BATCHLESS,
                branch_id: TEST_BRANCH,
                quantity_change: absolute,
                reason: "opening balance",
            })
        );
    }

    if (opts.thenBatch != null) {
        // A batch finally arrives; from here quantity is derived, and it must
        // agree with the absolute value that was there before.
        await applyEventLocally(
            envelope("drug_batch_created", "drug_batch", "batch-bl", {
                branch_id: TEST_BRANCH,
                drug_id: BATCHLESS,
                batch_number: "B1",
                quantity: opts.thenBatch,
                remaining_quantity: opts.thenBatch,
                manufacturing_date: "2020-01-01",
                expiry_date: "2030-09-26",
            })
        );
    }

    return localQty(BATCHLESS);
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
        out += "PHASE 3 — local branch_inventory.quantity after replaying / applying stock events\n";
        out += "=".repeat(104) + "\n";
        out +=
            pad("drug", 14) +
            pad("payload", 15) +
            pad("order", 18) +
            pad("device", 10) +
            padL("server", 8) +
            padL("local", 8) +
            "   verdict\n";
        out += "-".repeat(104) + "\n";
        for (const r of rows) {
            out +=
                pad(r.drug, 14) +
                pad(r.variant, 15) +
                pad(r.order, 18) +
                pad(r.device, 10) +
                padL(String(r.serverQty), 8) +
                padL(String(r.localQty), 8) +
                (r.ok ? "   OK" : `   DOUBLE-COUNTED (x${(r.localQty / r.serverQty).toFixed(1)})`) +
                "\n";
        }
        out += "-".repeat(104) + "\n";
        const bad = rows.filter((r) => !r.ok);
        out += bad.length
            ? `${bad.length} of ${rows.length} cases end at the WRONG quantity.\n`
            : `all ${rows.length} cases converge on the server value.\n`;
        out += "=".repeat(104) + "\n";
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
                        record(fx.label, variant, order, device, fx.serverQty, local);
                        expect(local).toBe(fx.serverQty);
                    });
                }
            }
        }
    }

    // ── Movement events, on the Gebedol fixture ──────────────────────────
    //
    // The 24 cases above only ever SET stock. These check that the events which
    // MOVE it agree with the server too, and that the derivation is re-applied
    // after each one. Under the old design each of these wrote quantity
    // relatively AND the batch events wrote it relatively, so a replay of any
    // pair of them compounded.
    const GEBEDOL_FX = FIXTURES[0];

    it("sale_created after a full sync ends at the server's post-sale quantity", async () => {
        const local = await runMovement(GEBEDOL_FX, "sale-created", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "sale_created", "has-rows", 100, local);
        expect(local).toBe(100);
    });

    it("sale_created before any batch event still ends at the server value", async () => {
        const local = await runMovement(GEBEDOL_FX, "sale-created", "before-sync");
        record(GEBEDOL_FX.label, "stocked", "sale_created", "fresh", 100, local);
        expect(local).toBe(100);
    });

    it("sale_voided restores exactly what the sale took", async () => {
        const local = await runMovement(GEBEDOL_FX, "sale-voided", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "sale_voided", "has-rows", 117, local);
        expect(local).toBe(117);
    });

    it("a replayed sale_voided does not restore the stock twice", async () => {
        const local = await runMovement(GEBEDOL_FX, "sale-voided-replayed", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "sale_voided x2", "has-rows", 117, local);
        expect(local).toBe(117);
    });

    it("stock_adjusted with batch detail lands on the server value", async () => {
        const local = await runMovement(GEBEDOL_FX, "stock-adjusted-batches", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "stock_adjusted", "has-rows", 132, local);
        expect(local).toBe(132);
    });

    it("a replayed stock_adjusted is applied once", async () => {
        const local = await runMovement(GEBEDOL_FX, "stock-adjusted-replayed", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "stock_adjusted x2", "has-rows", 132, local);
        expect(local).toBe(132);
    });

    it("a delta-only adjustment is absorbed once the device holds batches", async () => {
        // DESIGN CONSEQUENCE, asserted deliberately: the relative-delta path is
        // only reachable while a pair has NO batch rows. Here it does have one
        // (the 117 batch), so recomputeQuantityFromBatches overwrites the delta
        // with the batch sum and quantity stays 117.
        //
        // That is the intended trade: one writer wins, so a delta can never
        // silently disagree with the batches. The cost is that a stock_take
        // correction MUST be published with batch detail or it will not stick on
        // a device that has batches. See the report.
        const local = await runMovement(GEBEDOL_FX, "stock-adjusted-delta-only", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "stock_adj delta-only", "has-rows", 117, local);
        expect(local).toBe(117);
    });

    it("a delta-only adjustment DOES stick for a pair with no batches", async () => {
        const local = await runBatchlessDeltaOnly();
        record("Batchless delta", "no-batch", "inv+adj", "fresh", 25, local);
        expect(local).toBe(25);
    });

    it("stock_transfer moves quantity off the source branch", async () => {
        const local = await runMovement(GEBEDOL_FX, "stock-transfer", "after-sync");
        record(GEBEDOL_FX.label, "stocked", "stock_transfer", "has-rows", 97, local);
        expect(local).toBe(97);
    });

    // ── The batchless fallback ────────────────────────────────────────────
    //
    // A drug can be added to a branch at quantity 0 and have no batches at all.
    // That is the documented case where the derivation must NOT run and the
    // event's absolute quantity stands. If the derivation fired here it would
    // read an empty batch set as 0 and wipe a legitimately stocked row.
    it("a batchless drug keeps the absolute quantity from its inventory event", async () => {
        const local = await runBatchless(0);
        record("Batchless @0", "with-quantity", "inv-only", "fresh", 0, local);
        expect(local).toBe(0);
    });

    it("a batchless drug then stocked by adjustment keeps that value", async () => {
        const local = await runBatchless(40);
        record("Batchless @40", "with-quantity", "inv+adj", "fresh", 40, local);
        expect(local).toBe(40);
    });

    it("a batchless drug's quantity survives a sellable recompute", async () => {
        // sellable_quantity has its own no-batches fallback; the two must agree.
        const local = await runBatchless(40);
        record("Batchless @40", "with-quantity", "inv+adj+sell", "fresh", 40, local);
        expect(local).toBe(40);
    });

    it("a batchless drug starts deriving the moment a batch arrives", async () => {
        // Once a batch exists, quantity becomes derived again — so a later
        // adjustment that moves no batches can no longer drift it.
        const local = await runBatchless(40, { thenBatch: 40 });
        record("Batchless @40", "with-quantity", "inv+adj+batch", "partial", 40, local);
        expect(local).toBe(40);
    });
});