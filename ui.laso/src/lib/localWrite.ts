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
    PurchaseOrder, Customer, Prescription,
} from "@/types";

const SALE_COLUMNS = new Set([
    "id",
    "organization_id",
    "branch_id",
    "sale_number",
    "customer_id",
    "customer_name",
    "subtotal",
    "discount_amount",
    "tax_amount",
    "total_amount",
    "price_contract_id",
    "contract_name",
    "contract_discount_percentage",
    "payment_method",
    "payment_status",
    "amount_paid",
    "change_amount",
    "payment_reference",
    "prescription_id",
    "prescription_number",
    "prescriber_name",
    "cashier_id",
    "pharmacist_id",
    "insurance_claim_number",
    "patient_copay_amount",
    "insurance_covered_amount",
    "insurance_verified",
    "insurance_verified_at",
    "insurance_verified_by",
    "notes",
    "status",
    "cancelled_at",
    "cancelled_by",
    "cancellation_reason",
    "refund_amount",
    "refunded_at",
    "receipt_printed",
    "receipt_emailed",
    "items_json",
    "items_count",
    "sync_status",
    "sync_version",
    "synced_at",
    "updated_at",
    "created_at",
]);

const PURCHASE_ORDER_COLUMNS = new Set([
    "id",
    "organization_id",
    "branch_id",
    "po_number",
    "supplier_id",
    "subtotal",
    "tax_amount",
    "shipping_cost",
    "total_amount",
    "status",
    "ordered_by",
    "approved_by",
    "approved_at",
    "expected_delivery_date",
    "received_date",
    "notes",
    "items_json",
    "sync_status",
    "sync_version",
    "synced_at",
    "updated_at",
    "created_at",
]);

const PRESCRIPTION_COLUMNS = new Set([
    "id",
    "organization_id",
    "branch_id",
    "prescription_number",
    "customer_id",
    "prescriber_name",
    "prescriber_license",
    "prescriber_phone",
    "prescriber_address",
    "issue_date",
    "expiry_date",
    "medications",
    "diagnosis",
    "notes",
    "special_instructions",
    "refills_allowed",
    "refills_remaining",
    "last_refill_date",
    "status",
    "verified_by",
    "verified_at",
    "created_offline_at",
    "sync_status",
    "sync_version",
    "synced_at",
    "updated_at",
    "created_at",
]);

const CUSTOMER_COLUMNS = new Set([
    "id",
    "organization_id",
    "customer_type",
    "first_name",
    "last_name",
    "phone",
    "email",
    "date_of_birth",
    "loyalty_points",
    "loyalty_tier",
    "insurance_provider_id",
    "insurance_member_id",
    "preferred_contract_id",
    "is_active",
    "is_deleted",
    "sync_status",
    "sync_version",
    "synced_at",
    "updated_at",
    "created_at",
]);

function pickColumns(
    record: Record<string, unknown>,
    columns: Set<string>
): Record<string, unknown> {
    return Object.fromEntries(
        Object.entries(record).filter(([key]) => columns.has(key))
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// INTERNAL — upsert a row and enqueue it
// ─────────────────────────────────────────────────────────────────────────────

async function upsertAndEnqueue<T extends Record<string, unknown>>(
    table: string,
    record: T,
    operation: "create" | "update" = "create",
    queueExtras: Record<string, unknown> = {}
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

    await enqueue(table, id, operation, syncVersion, {
        ...payload,
        ...queueExtras,
    } as Record<string, unknown>);
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
        const sanitizeNumber = (val: unknown, fallback = 0): number => {
            const num = typeof val === 'number' ? val : Number(val);
            return !isNaN(num) ? num : fallback;
        };
        
        const rawPayload = {
            ...saleData,
            discount_amount: sanitizeNumber((saleData as Record<string, unknown>).discount_amount ??
                (saleData as Record<string, unknown>).total_discount_amount ?? 0),
            tax_amount: sanitizeNumber((saleData as Record<string, unknown>).tax_amount),
            subtotal: sanitizeNumber((saleData as Record<string, unknown>).subtotal),
            total_amount: sanitizeNumber((saleData as Record<string, unknown>).total_amount),
            items_json: JSON.stringify(items ?? []),
            items_count: (items ?? []).reduce(
                (sum, item) => sum + (item?.quantity ?? 0),
                0
            ),
        };
        const payload = pickColumns(rawPayload as Record<string, unknown>, SALE_COLUMNS);
        await upsertAndEnqueue(
            "sales",
            payload as Record<string, unknown>,
            "create",
            {
                items: items ?? [],
                sync_protocol_version: 2,
            }
        );
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
            "create",
            { sync_protocol_version: 2 }
        );
    },

    /**
     * Update branch inventory quantity.
     * Call after a local sale or adjustment to keep the local count accurate.
     */
    inventory: async (
        branchId: string,
        drugId: string,
        quantityDelta: number,
        enqueueForSync = true
    ): Promise<void> => {
        const db = await getDb();
        const now = new Date().toISOString();

        const result = await db.execute(
            enqueueForSync
                ? `UPDATE branch_inventory
                   SET quantity = quantity + $1,
                       sync_status = 'pending',
                       sync_version = sync_version + 1,
                       updated_at = $2
                   WHERE branch_id = $3
                     AND drug_id = $4
                     AND quantity + $1 >= 0`
                : `UPDATE branch_inventory
                   SET quantity = quantity + $1,
                       updated_at = $2
                   WHERE branch_id = $3
                     AND drug_id = $4
                     AND quantity + $1 >= 0`,
            [quantityDelta, now, branchId, drugId]
        );

        if (result.rowsAffected === 0) {
            const existing = await db.select<{ id: string; quantity: number }[]>(
                `SELECT id, quantity
                 FROM branch_inventory
                 WHERE branch_id = $1 AND drug_id = $2
                 LIMIT 1`,
                [branchId, drugId]
            );

            if (existing.length > 0) {
                throw new Error(
                    `Insufficient local stock for drug ${drugId}. ` +
                    `Available: ${existing[0].quantity}, requested change: ${quantityDelta}.`
                );
            }

            if (quantityDelta < 0) {
                throw new Error(
                    `No local inventory record exists for drug ${drugId} at branch ${branchId}.`
                );
            }

            // No row yet — create one only for non-negative stock additions.
            const id = crypto.randomUUID();
            if (enqueueForSync) {
                await upsertAndEnqueue("branch_inventory", {
                    id,
                    branch_id: branchId,
                    drug_id: drugId,
                    quantity: quantityDelta,
                    reserved_quantity: 0,
                    location: null,
                    selling_price: null,
                    updated_at: now,
                    created_at: now,
                }, "create");
            } else {
                await db.execute(
                    `INSERT INTO branch_inventory (
                       id, branch_id, drug_id, quantity, reserved_quantity,
                       location, selling_price, sync_status, sync_version,
                       synced_at, updated_at, created_at
                     ) VALUES ($1, $2, $3, $4, 0, NULL, NULL, 'synced', 1, NULL, $5, $5)`,
                    [id, branchId, drugId, quantityDelta, now]
                );
            }
            return;
        }

        // Enqueue the updated row
        const [row] = await db.select<Record<string, unknown>[]>(
            "SELECT * FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2",
            [branchId, drugId]
        );
        if (row && enqueueForSync) {
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
            adjustment.quantity_change,
            false
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
            operation,
            { items: items ?? [] }
        );
    },

    cachePurchaseOrders: async (orders: PurchaseOrder[]): Promise<void> => {
        if (orders.length === 0) return;

        const db = await getDb();
        for (const order of orders) {
            const existing = await db.select<{ sync_status: string; po_number: string }[]>(
                "SELECT sync_status, po_number FROM purchase_orders WHERE id = $1 LIMIT 1",
                [order.id]
            );
            const localStatus = existing[0]?.sync_status;
            const isOfflineDraft = existing[0]?.po_number?.startsWith("OFFLINE-PO-") ?? false;
            if ((localStatus === "pending" || localStatus === "conflict") && isOfflineDraft) {
                continue;
            }

            const payload = pickColumns(
                {
                    ...order,
                    items_json: "[]",
                    sync_status: "synced",
                    synced_at: order.synced_at ?? new Date().toISOString(),
                } as Record<string, unknown>,
                PURCHASE_ORDER_COLUMNS
            );
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
                `INSERT INTO purchase_orders (${cols.join(", ")}) VALUES (${placeholders})
                 ON CONFLICT(id) DO UPDATE SET ${updates}`,
                vals
            );
        }
    },

    cachePrescriptions: async (prescriptions: Prescription[]): Promise<void> => {
        if (prescriptions.length === 0) return;

        const db = await getDb();
        for (const prescription of prescriptions) {
            const existing = await db.select<{ sync_status: string }[]>(
                "SELECT sync_status FROM prescriptions WHERE id = $1 LIMIT 1",
                [prescription.id]
            );
            const localStatus = existing[0]?.sync_status;
            if (localStatus === "pending" || localStatus === "conflict") {
                continue;
            }

            const payload = pickColumns(
                {
                    ...prescription,
                    medications: JSON.stringify(prescription.medications ?? []),
                    created_offline_at: prescription.created_offline_at ?? null,
                    sync_status: "synced",
                    synced_at: prescription.synced_at ?? new Date().toISOString(),
                } as Record<string, unknown>,
                PRESCRIPTION_COLUMNS
            );
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
                `INSERT INTO prescriptions (${cols.join(", ")}) VALUES (${placeholders})
                 ON CONFLICT(id) DO UPDATE SET ${updates}`,
                vals
            );
        }
    },

    prescription: async (
        prescription: Omit<Prescription, "sync_status" | "sync_version"> & { id: string },
        operation: "create" | "update" = "create"
    ): Promise<void> => {
        const now = new Date().toISOString();
        const payload = pickColumns(
            {
                ...prescription,
                medications: JSON.stringify(prescription.medications ?? []),
                refills_remaining: prescription.refills_remaining ?? prescription.refills_allowed ?? 0,
                status: prescription.status ?? "active",
                created_offline_at: prescription.created_offline_at ?? now,
                updated_at: prescription.updated_at ?? now,
                created_at: prescription.created_at ?? now,
            } as Record<string, unknown>,
            PRESCRIPTION_COLUMNS
        );

        await upsertAndEnqueue("prescriptions", payload, operation);
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

        const payload = pickColumns(
            {
                ...customerData,
                updated_at: now,
                created_at: customerData.created_at ?? now,
            } as Record<string, unknown>,
            CUSTOMER_COLUMNS
        );

        await upsertAndEnqueue("customers", payload, operation);
    },
};
