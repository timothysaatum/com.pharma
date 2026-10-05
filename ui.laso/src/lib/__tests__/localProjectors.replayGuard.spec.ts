/**
 * Phase 2 — idempotency of the local projectors under replay.
 *
 * The bug
 * -------
 * Rewinding the cursor to 0 (the stranded-cursor self-heal) replays an
 * organisation's whole event log. Most projectors survive that: they upsert, or
 * check for existence first. Four do not — they apply raw relative mutations:
 *
 *   sale_voided              quantity = quantity + n   (restores stock)
 *   prescription_refill_used refills_remaining = refills_remaining - 1
 *   stock_adjusted           quantity = MAX(0, quantity + delta)
 *   stock_transfer           quantity ± n on two branches
 *
 * Delivered twice, each double-applies. A rewind that silently inflated stock
 * or drained refill allowances would be far worse than the silent no-op it
 * replaced, so these four consult `applied_events` first (migration v34) and
 * skip an event they have already applied.
 *
 * A separate guard is tested here too: `_branchInventoryUpserted` read a
 * MISSING `quantity` as 0, so a branch_inventory event that carries only
 * branch-owned metadata (which is what the device's own
 * buildBranchInventoryEnvelope emits) ZEROED existing stock on projection.
 */
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
    GEBEDOL,
    OTHER_BRANCH,
    TEST_BRANCH,
    daysFromNow,
    insertBatch,
    insertInventory,
    installRealDb,
    rawDb,
    resetTables,
} from "./realDb";
import type { EventEnvelope } from "@/lib/eventEnvelope";

let applyEventLocally: (e: EventEnvelope) => Promise<void>;

function envelope(over: Partial<EventEnvelope> & { event_type: string }): EventEnvelope {
    return {
        event_id: "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        org_id: "2d060ef8-a302-447c-91f4-b2fd30268341",
        seq: 1,
        aggregate_id: "agg-1",
        aggregate_type: "sale",
        schema_version: 1,
        payload: {},
        dependencies: [],
        authored_at: "2026-01-01T00:00:00Z",
        authored_by: "bae475d9-994a-4d5b-abb2-32aa4b082602",
        branch_id: TEST_BRANCH,
        hash_self: "0".repeat(64),
        hash_prev: "0".repeat(64),
        received_at: "2026-01-01T00:00:00Z",
        ...over,
    } as EventEnvelope;
}

function qtyOf(branchId: string, drugId: string): number {
    const row = rawDb()
        .prepare("SELECT quantity FROM branch_inventory WHERE branch_id = ? AND drug_id = ?")
        .get(branchId, drugId) as { quantity: number } | undefined;
    return row?.quantity ?? 0;
}

function refillCount(prescriptionId: string): number {
    const row = rawDb()
        .prepare("SELECT refills_remaining FROM prescriptions WHERE id = ?")
        .get(prescriptionId) as { refills_remaining: number } | undefined;
    return row?.refills_remaining ?? -1;
}

describe("Phase 2 — projector replay safety", () => {
    beforeAll(async () => {
        await installRealDb();
        ({ applyEventLocally } = await import("@/lib/localProjectors"));
    });

    beforeEach(() => {
        resetTables([
            "applied_events",
            "branch_inventory",
            "drug_batches",
            "prescriptions",
            "sales",
        ]);
    });

    // ── sale_voided ────────────────────────────────────────────────────────

    it("restores stock once, not twice, when sale_voided is replayed", async () => {
        // quantity MUST equal the batch sum for this pair: quantity is derived
        // from the batches wherever any exist (see recomputeQuantityFromBatches),
        // so a fixture with quantity 40 against a 100 batch would be asserting
        // against a state the invariant says cannot exist.
        insertInventory(rawDb(), { quantity: 100 });
        insertBatch(rawDb(), { remaining: 100 });

        const e = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5FAW",
            event_type: "sale_voided",
            aggregate_type: "sale",
            aggregate_id: "sale-1",
            payload: {
                drug_id: GEBEDOL,
                quantity: 12,
                branch_id: TEST_BRANCH,
                batch_changes: [{ batch_id: "batch-1", quantity_used: 12 }],
            },
        });

        await applyEventLocally(e);
        // The batch is the fact (100 -> 112); quantity follows it.
        expect(insertBatchRemaining()).toBe(112);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(112);

        // The server re-delivers the same event (rewind, retry, duplicate push).
        await applyEventLocally(e);
        // Unchanged: a second restore would be stock that never left.
        expect(insertBatchRemaining()).toBe(112);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(112);
    });

    // ── prescription_refill_used ───────────────────────────────────────────

    it("consumes one refill, not two, when prescription_refill_used is replayed", async () => {
        rawDb()
            .prepare(
                `INSERT INTO prescriptions
                   (id, organization_id, branch_id, prescription_number,
                    customer_id, prescriber_name, prescriber_license,
                    issue_date, expiry_date, medications, refills_allowed,
                    refills_remaining, status, created_at, updated_at)
                 VALUES ('rx-1','2d060ef8-a302-447c-91f4-b2fd30268341','br-1',
                         'RX-1','cust-1','Dr Test','LIC-1',
                         '2026-01-01','2027-01-01','[]',3,3,'active',
                         '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`
            )
            .run();

        const e = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5FAX",
            event_type: "prescription_refill_used",
            aggregate_type: "prescription",
            aggregate_id: "rx-1",
            payload: { refill_number: 1 },
        });

        await applyEventLocally(e);
        expect(refillCount("rx-1")).toBe(2);

        await applyEventLocally(e);
        expect(refillCount("rx-1")).toBe(2);
    });

    // ── stock_adjusted ─────────────────────────────────────────────────────

    it("applies a stock adjustment once, not twice, when replayed", async () => {
        insertInventory(rawDb(), { quantity: 20 });

        const e = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5FAY",
            event_type: "stock_adjusted",
            aggregate_type: "stock",
            aggregate_id: "stk-1",
            payload: {
                drug_id: GEBEDOL,
                branch_id: TEST_BRANCH,
                quantity_change: 15,
                reason: "count correction",
            },
        });

        await applyEventLocally(e);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(35);

        await applyEventLocally(e);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(35);
    });

    // ── stock_transfer ─────────────────────────────────────────────────────

    it("moves stock once across both branches when stock_transfer is replayed", async () => {
        insertInventory(rawDb(), { id: "src", branchId: TEST_BRANCH, quantity: 50 });
        insertInventory(rawDb(), { id: "dst", branchId: OTHER_BRANCH, quantity: 5 });

        const e = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAV",
            event_type: "stock_transfer",
            aggregate_type: "stock",
            aggregate_id: "stk-2",
            payload: {
                drug_id: GEBEDOL,
                source_branch_id: TEST_BRANCH,
                destination_branch_id: OTHER_BRANCH,
                quantity: 20,
                batch_changes: [],
            },
        });

        await applyEventLocally(e);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(30);
        expect(qtyOf(OTHER_BRANCH, GEBEDOL)).toBe(25);

        await applyEventLocally(e);
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(30);
        expect(qtyOf(OTHER_BRANCH, GEBEDOL)).toBe(25);
    });

    // ── the guard must not suppress a genuinely new event ──────────────────

    it("still applies a distinct guarded event of the same type", async () => {
        insertInventory(rawDb(), { quantity: 10 });

        const first = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAW",
            event_type: "stock_adjusted",
            aggregate_id: "stk-1",
            payload: { drug_id: GEBEDOL, branch_id: TEST_BRANCH, quantity_change: 5 },
        });
        const second = envelope({
            // Different event_id, same type: a second real adjustment.
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAX",
            seq: 2,
            event_type: "stock_adjusted",
            aggregate_id: "stk-1",
            payload: { drug_id: GEBEDOL, branch_id: TEST_BRANCH, quantity_change: 7 },
        });

        await applyEventLocally(first);
        await applyEventLocally(second);

        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(22);
    });

    it("replays an unguarded event type freely (upserts stay idempotent)", async () => {
        const e = envelope({
            event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAY",
            event_type: "drug_created",
            aggregate_type: "drug",
            aggregate_id: GEBEDOL,
            payload: { name: "Amoxicilin 500mg", reorder_quantity: 0, unit_price: "12.00" },
        });

        await applyEventLocally(e);
        await applyEventLocally(e);

        const row = rawDb()
            .prepare("SELECT name, reorder_quantity FROM drugs WHERE id = ?")
            .get(GEBEDOL) as { name: string; reorder_quantity: number };
        expect(row.name).toBe("Amoxicilin 500mg");
        // An upsert, so a replay is naturally a no-op — no marker needed.
        expect(row.reorder_quantity).toBe(0);
        const markers = rawDb()
            .prepare("SELECT COUNT(*) AS n FROM applied_events")
            .get() as { n: number };
        expect(markers.n).toBe(0);
    });

    // ── the guard marker must not outlive a failed projection ──────────────

    it("writes no guard row when the projector bails out before mutating", async () => {
        // Ported from origin/fix/stock-sync's replayGuards.test.ts
        // ("rolls back the guard row when the projection throws"), which I did
        // not have. A guard row written for an event that never actually
        // applied is worse than no guard at all: it permanently suppresses the
        // real projection on the next replay.
        insertInventory(rawDb(), { quantity: 100 });

        // A transfer with no destination branch makes _stockTransfer return
        // before touching any stock.
        await applyEventLocally(
            envelope({
                event_id: "01ARZ3NDEKTSV4RRFFQ69G5HAZ",
                event_type: "stock_transfer",
                aggregate_type: "stock",
                aggregate_id: "stk-3",
                payload: {
                    drug_id: GEBEDOL,
                    source_branch_id: TEST_BRANCH,
                    quantity: 5,
                    batch_changes: [],
                },
            })
        );

        const markers = rawDb()
            .prepare("SELECT event_id FROM applied_events WHERE event_id = ?")
            .all("01ARZ3NDEKTSV4RRFFQ69G5HAZ") as unknown[];
        expect(markers).toHaveLength(0);
        // And the stock really was untouched.
        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(100);
    });

    // ── branch_inventory quantity guard ────────────────────────────────────

    it("leaves stock untouched when a branch_inventory event states no quantity", async () => {
        insertInventory(rawDb(), { quantity: 247 });

        // The device's own branch_inventory envelope carries branch-owned
        // metadata only — no quantity. Reading the absence as 0 destroyed stock.
        await applyEventLocally(
            envelope({
                event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAA",
                event_type: "branch_inventory_updated",
                aggregate_type: "branch_inventory",
                aggregate_id: "inv-1",
                payload: {
                    branch_id: TEST_BRANCH,
                    drug_id: GEBEDOL,
                    location: "Aisle 3",
                    selling_price: "12.00",
                },
            })
        );

        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(247);
        // The metadata it DID carry is applied.
        const row = rawDb()
            .prepare("SELECT location FROM branch_inventory WHERE id = 'inv-1'")
            .get() as { location: string };
        expect(row.location).toBe("Aisle 3");
    });

    it("still writes an absolute quantity when the event states one", async () => {
        insertInventory(rawDb(), { quantity: 247 });

        await applyEventLocally(
            envelope({
                event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAB",
                event_type: "branch_inventory_updated",
                aggregate_type: "branch_inventory",
                aggregate_id: "inv-1",
                payload: {
                    branch_id: TEST_BRANCH,
                    drug_id: GEBEDOL,
                    quantity: 300,
                    selling_price: "12.00",
                },
            })
        );

        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(300);
    });

    it("inserts with a 0 default when no row exists yet", async () => {
        await applyEventLocally(
            envelope({
                event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAC",
                event_type: "branch_inventory_updated",
                aggregate_type: "branch_inventory",
                aggregate_id: "inv-new",
                payload: { branch_id: TEST_BRANCH, drug_id: GEBEDOL, location: "Aisle 1" },
            })
        );

        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(0);
    });

    it("accepts sellable_quantity as the stated quantity", async () => {
        insertInventory(rawDb(), { quantity: 5 });

        await applyEventLocally(
            envelope({
                event_id: "01ARZ3NDEKTSV4RRFFQ69G5GAD",
                event_type: "branch_inventory_updated",
                aggregate_type: "branch_inventory",
                aggregate_id: "inv-1",
                payload: { branch_id: TEST_BRANCH, drug_id: GEBEDOL, sellable_quantity: 42 },
            })
        );

        expect(qtyOf(TEST_BRANCH, GEBEDOL)).toBe(42);
    });
});

function insertBatchRemaining(): number {
    const row = rawDb()
        .prepare("SELECT remaining_quantity FROM drug_batches WHERE id = 'batch-1'")
        .get() as { remaining_quantity: number } | undefined;
    return row?.remaining_quantity ?? -1;
}

// Keep the unused import honest: daysFromNow is part of the shared helper
// surface used by insertBatch's default expiry.
void daysFromNow;