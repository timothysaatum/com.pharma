/**
 * localWrite.ts
 * =============
 * Helpers for writing branch-owned records to local SQLite
 * and automatically enqueuing them for sync push.
 *
 * Usage pattern (e.g. when cashier processes a sale offline):
 *
 *   import { writeLocal } from "@/lib/localWrite";
 *
 *   const sale = { id: crypto.randomUUID(), ... };
 *   await writeLocal.sale(sale);
 *   // → written to local DB + added to sync_queue
 *
 * The sync engine picks up sync_queue entries on next online cycle.
 */

import { getDb, enqueue } from "@/lib/localDb";
import type {
    Sale, DrugBatch, StockAdjustmentCreate,
    PurchaseOrder, Customer,
} from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// INTERNAL — upsert a row and enqueue it
// ─────────────────────────────────────────────────────────────────────────────

async function upsertAndEnqueue<T extends Record<string, unknown>>(
    table: string,
    record: T,
    operation: "create" | "update" = "create"
): Promise<void> {
    const db = await getDb();
    const id = record.id as string;
    const syncVersion = (record.sync_version as number | undefined) ?? 1;
    const now = new Date().toISOString();

    const payload = {
        ...record,
        sync_status: "pending",
        sync_version: syncVersion,
        updated_at: now,
        created_at: record.created_at ?? now,
    };

    const cols = Object.keys(payload);
    const vals = cols.map((c) => {
        const v = payload[c];
        if (typeof v === "boolean") return v ? 1 : 0;
        if (Array.isArray(v) || (typeof v === "object" && v !== null)) return JSON.stringify(v);
        return v ?? null;
    });
    const placeholders = cols.map((_, i) => `$${i + 1}`).join(", ");
    const updates = cols
        .filter((c) => c !== "id")
        .map((c) => `${c} = excluded.${c}`)
        .join(", ");

    await db.execute(
        `INSERT INTO ${table} (${cols.join(", ")}) VALUES (${placeholders})
         ON CONFLICT(id) DO UPDATE SET ${updates}`,
        vals
    );

    await enqueue(table, id, operation, syncVersion, payload as Record<string, unknown>);
}

// ─────────────────────────────────────────────────────────────────────────────
// PUBLIC API
// ─────────────────────────────────────────────────────────────────────────────

export const writeLocal = {
    /**
     * Record a completed sale locally.
     *
     * FIX: `items` (SaleItem[]) is destructured out before the spread —
     * there is no `items` column in SQLite.  The array is serialised as
     * `items_json` (TEXT) so offline receipt rendering can read it back
     * without hitting the server.
     */
    sale: async (
        sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string }
    ): Promise<void> => {
        const { items, ...saleData } = sale;
        const payload = {
            ...saleData,
            items_json: JSON.stringify(items ?? []),
        };
        await upsertAndEnqueue("sales", payload as Record<string, unknown>, "create");
    },

    /**
     * Add a new drug batch (received stock).
     *
     * FIX: client-computed convenience fields (`days_until_expiry`,
     * `is_expired`, `is_expiring_soon`) are destructured out — they have
     * no SQLite columns and are derived at read time.
     */
    drugBatch: async (
        batch: Omit<DrugBatch, "sync_status" | "sync_version"> & { id: string }
    ): Promise<void> => {
        const { days_until_expiry, is_expired, is_expiring_soon, ...batchData } = batch;
        await upsertAndEnqueue(
            "drug_batches",
            batchData as Record<string, unknown>,
            "create"
        );
    },

    /**
     * Update branch inventory quantity.
     * Call after a local sale or adjustment to keep the local count accurate.
     */
    inventory: async (
        branchId: string,
        drugId: string,
        quantityDelta: number
    ): Promise<void> => {
        const db = await getDb();
        const now = new Date().toISOString();

        const result = await db.execute(
            `UPDATE branch_inventory
             SET quantity    = MAX(0, quantity + $1),
                 sync_status = 'pending',
                 updated_at  = $2
             WHERE branch_id = $3 AND drug_id = $4`,
            [quantityDelta, now, branchId, drugId]
        );

        if (result.rowsAffected === 0) {
            // No row yet — create one
            const id = crypto.randomUUID();
            await upsertAndEnqueue("branch_inventory", {
                id,
                branch_id: branchId,
                drug_id: drugId,
                quantity: Math.max(0, quantityDelta),
                reserved_quantity: 0,
                location: null,
                selling_price: null,
                updated_at: now,
                created_at: now,
            }, "create");
            return;
        }

        // Enqueue the updated row
        const [row] = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2",
            [branchId, drugId]
        );
        if (row) {
            await enqueue(
                "branch_inventory",
                row.id as string,
                "update",
                (row.sync_version as number) ?? 1,
                row
            );
        }
    },

    /**
     * Record a stock adjustment locally.
     *
     * FIX: the `stock_adjustments` table does not exist in the local SQLite
     * schema (StockAdjustment has no SyncTrackingMixin on the server and is
     * write-once/immutable).  We skip the local table INSERT and go straight
     * to the sync queue — the server becomes the source of truth on next push.
     *
     * The local inventory count is still decremented immediately so the POS
     * reflects the correct stock without waiting for a sync cycle.
     */
    stockAdjustment: async (
        adjustment: StockAdjustmentCreate & { id: string; adjusted_by: string }
    ): Promise<void> => {
        const now = new Date().toISOString();
        const payload = {
            ...adjustment,
            updated_at: now,
            created_at: now,
        };

        // Push-only: write directly to the queue, bypass local table INSERT
        await enqueue(
            "stock_adjustments",
            adjustment.id,
            "create",
            1,
            payload as Record<string, unknown>
        );

        // Reflect the stock change locally so the POS is immediately accurate
        await writeLocal.inventory(
            adjustment.branch_id,
            adjustment.drug_id,
            adjustment.quantity_change
        );
    },

    /**
     * Create or update a purchase order locally.
     *
     * FIX: `items` (PurchaseOrderItem[]) is destructured out — there is no
     * `items` column in SQLite.  The array is serialised as `items_json`.
     */
    purchaseOrder: async (
        po: Omit<PurchaseOrder, "sync_status" | "sync_version"> & { id: string; items?: unknown[] },
        operation: "create" | "update" = "create"
    ): Promise<void> => {
        const { items, ...poData } = po;
        const payload = {
            ...poData,
            items_json: JSON.stringify(items ?? []),
        };
        await upsertAndEnqueue(
            "purchase_orders",
            payload as Record<string, unknown>,
            operation
        );
    },

    /**
     * Create a customer locally (org-level but created offline at branch).
     * Will be deduped by phone/email on the server when pushed.
     *
     * FIX: several Customer fields have no SQLite columns and are destructured
     * out before the spread:
     *   - allergies, chronic_conditions, preferred_contact_method,
     *     marketing_consent, insurance_card_image_url, address
     *     → not in the local customer schema (omitted to keep it lightweight)
     *   - deleted_at, deleted_by
     *     → SoftDeleteFields present on the type but not in the SQLite schema
     */
    customer: async (
        customer: Omit<Customer, "sync_status" | "sync_version"> & { id: string },
        operation: "create" | "update" = "create"
    ): Promise<void> => {
        const now = new Date().toISOString();

        const {
            allergies,
            chronic_conditions,
            preferred_contact_method,
            marketing_consent,
            insurance_card_image_url,
            address,
            deleted_at,
            deleted_by,
            ...customerData
        } = customer;

        await upsertAndEnqueue("customers", {
            ...customerData,
            updated_at: now,
            created_at: customerData.created_at ?? now,
        }, operation);
    },
};
