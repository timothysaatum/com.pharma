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

import { getDb, enqueue, appendToOutbox, getOutboxTailHash, getVersionVector, bumpVector } from "@/lib/localDb";
import { generateUlid, computeHashSelf, GENESIS_HASH } from "@/lib/eventEnvelope";
import type { OutboxEvent, VectorClock } from "@/lib/localDb";
import type {
    Sale, DrugBatch, StockAdjustmentCreate,
    PurchaseOrder, Customer, Prescription, BranchInventory,
} from "@/types";

const AUDIT_LOG_COLUMNS = new Set([
    "id",
    "organization_id",
    "user_id",
    "user_full_name",
    "action",
    "entity_type",
    "entity_id",
    "changes",
    "ip_address",
    "user_agent",
    "context_metadata",
    "created_at",
    "updated_at",
    "sync_status",
    "sync_version",
    "last_synced_at",
    "sync_hash",
]);

export const SALE_COLUMNS = new Set([
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
    "contract_type",
    "payment_method",
    "payment_status",
    "amount_paid",
    "change_amount",
    "payment_reference",
    "split_payment_details",
    "insurance_preauth_number",
    "prescription_id",
    "prescription_number",
    "prescriber_name",
    "prescriber_license",
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
    "refunded_by",
    "refund_reason",
    "refund_reference",
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

export function buildLocalSalePayload(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: Sale["items"] = sale.items ?? [],
    now = new Date().toISOString(),
): Record<string, unknown> {
    const { items: _items, ...saleData } = sale;
    const sanitizeNumber = (val: unknown, fallback = 0): number => {
        const num = typeof val === "number" ? val : Number(val);
        return Number.isFinite(num) ? num : fallback;
    };
    const rawPayload = {
        ...saleData,
        discount_amount: sanitizeNumber(
            (saleData as Record<string, unknown>).discount_amount
            ?? (saleData as Record<string, unknown>).total_discount_amount
            ?? 0,
        ),
        tax_amount: sanitizeNumber((saleData as Record<string, unknown>).tax_amount),
        subtotal: sanitizeNumber((saleData as Record<string, unknown>).subtotal),
        total_amount: sanitizeNumber((saleData as Record<string, unknown>).total_amount),
        items_json: JSON.stringify(items),
        items_count: items.reduce((sum, item) => sum + (item?.quantity ?? 0), 0),
        sync_status: "pending",
        sync_version: 1,
        updated_at: now,
        created_at: sale.created_at ?? now,
    };
    return pickColumns(rawPayload as Record<string, unknown>, SALE_COLUMNS);
}

// Serializes all outbox read-hash + insert pairs so concurrent mutations
// can't race to claim the same hash_prev and break the chain.
let _outboxLock: Promise<void> = Promise.resolve();
async function appendOutboxEvent(
    build: (hashPrev: string) => Promise<OutboxEvent>
): Promise<void> {
    _outboxLock = _outboxLock.then(async () => {
        const hashPrev = (await getOutboxTailHash()) ?? GENESIS_HASH;
        const envelope = await build(hashPrev);
        await appendToOutbox(envelope);
    });
    return _outboxLock;
}

async function buildSaleCreatedEnvelope(
    sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string },
    items: Sale["items"],
    now: string,
    hashPrev: string,
): Promise<OutboxEvent> {
    const eventId = generateUlid();

    const payload: Record<string, unknown> = {
        organization_id: sale.organization_id,
        branch_id: sale.branch_id,
        sale_number: sale.sale_number,
        customer_id: sale.customer_id ?? null,
        customer_name: sale.customer_name ?? null,
        cashier_id: sale.cashier_id,
        pharmacist_id: (sale as Record<string, unknown>).pharmacist_id ?? null,
        payment_method: sale.payment_method,
        payment_status: sale.payment_status,
        total_amount: Number(sale.total_amount ?? 0),
        subtotal: Number((sale as Record<string, unknown>).subtotal ?? 0),
        discount_amount: Number(
            (sale as Record<string, unknown>).discount_amount
            ?? (sale as Record<string, unknown>).total_discount_amount
            ?? 0,
        ),
        tax_amount: Number((sale as Record<string, unknown>).tax_amount ?? 0),
        amount_paid: Number(sale.amount_paid ?? 0),
        change_amount: Number((sale as Record<string, unknown>).change_amount ?? 0),
        prescription_id: sale.prescription_id ?? null,
        items: (items ?? []).map((item) => ({
            drug_id: item.drug_id,
            batch_id: item.batch_id ?? null,
            drug_name: item.drug_name ?? null,
            quantity: item.quantity,
            unit_price: item.unit_price,
            discount_amount: item.discount_amount ?? 0,
            subtotal: item.subtotal,
        })),
    };

    const hashSelf = await computeHashSelf(
        {
            event_id: eventId, aggregate_id: sale.id, aggregate_type: "sale",
            event_type: "sale_created", schema_version: 1, payload,
            dependencies: [], authored_at: now, authored_by: sale.cashier_id,
            branch_id: sale.branch_id, org_id: sale.organization_id,
        },
        hashPrev,
    );

    return {
        event_id: eventId,
        aggregate_id: sale.id,
        aggregate_type: "sale",
        event_type: "sale_created",
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: now,
        authored_by: sale.cashier_id,
        branch_id: sale.branch_id,
        org_id: sale.organization_id,
        hash_prev: hashPrev,
        hash_self: hashSelf,
        status: "pending",
        attempts: 0,
        error_code: null,
        error_message: null,
    };
}

async function buildPrescriptionEnvelope(
    prescription: Omit<Prescription, "sync_status" | "sync_version"> & { id: string },
    operation: "create" | "update",
    now: string,
    hashPrev: string,
): Promise<OutboxEvent> {
    const eventId = generateUlid();
    const eventType = operation === "create" ? "prescription_created" : "prescription_updated";
    const authoredBy = prescription.verified_by ?? prescription.organization_id;

    const payload: Record<string, unknown> = {
        organization_id: prescription.organization_id,
        branch_id: prescription.branch_id,
        prescription_number: prescription.prescription_number,
        customer_id: prescription.customer_id,
        prescriber_name: prescription.prescriber_name,
        prescriber_license: prescription.prescriber_license,
        prescriber_phone: prescription.prescriber_phone ?? null,
        prescriber_address: prescription.prescriber_address ?? null,
        issue_date: prescription.issue_date,
        expiry_date: prescription.expiry_date,
        medications: prescription.medications ?? [],
        diagnosis: prescription.diagnosis ?? null,
        notes: prescription.notes ?? null,
        special_instructions: prescription.special_instructions ?? null,
        refills_allowed: prescription.refills_allowed,
        refills_remaining: prescription.refills_remaining ?? prescription.refills_allowed ?? 0,
        status: prescription.status ?? "active",
        verified_by: prescription.verified_by ?? null,
    };

    const hashSelf = await computeHashSelf(
        {
            event_id: eventId, aggregate_id: prescription.id, aggregate_type: "prescription",
            event_type: eventType, schema_version: 1, payload,
            dependencies: [], authored_at: now, authored_by: authoredBy,
            branch_id: prescription.branch_id, org_id: prescription.organization_id,
        },
        hashPrev,
    );

    return {
        event_id: eventId,
        aggregate_id: prescription.id,
        aggregate_type: "prescription",
        event_type: eventType,
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: now,
        authored_by: authoredBy,
        branch_id: prescription.branch_id,
        org_id: prescription.organization_id,
        hash_prev: hashPrev,
        hash_self: hashSelf,
        status: "pending",
        attempts: 0,
        error_code: null,
        error_message: null,
    };
}

async function buildCustomerEnvelope(
    customer: Omit<Customer, "sync_status" | "sync_version"> & { id: string },
    branchId: string,
    now: string,
    operation: "create" | "update",
    hashPrev: string,
): Promise<OutboxEvent> {
    const eventId = generateUlid();
    const eventType = operation === "create" ? "customer_created" : "customer_updated";

    // For updates, read the current vector and bump this branch's component so
    // the server can detect concurrent edits from other branches.
    let versionVector: VectorClock = {};
    if (operation === "update") {
        const current = await getVersionVector("customers", customer.id);
        versionVector = bumpVector(current, branchId);
    }

    const payload: Record<string, unknown> = {
        organization_id: customer.organization_id,
        customer_type: customer.customer_type,
        first_name: customer.first_name ?? null,
        last_name: customer.last_name ?? null,
        phone: customer.phone ?? null,
        email: customer.email ?? null,
        date_of_birth: customer.date_of_birth ?? null,
        loyalty_points: customer.loyalty_points ?? 0,
        loyalty_tier: customer.loyalty_tier,
        insurance_provider_id: customer.insurance_provider_id ?? null,
        insurance_member_id: customer.insurance_member_id ?? null,
        allergies: customer.allergies ?? [],
        chronic_conditions: customer.chronic_conditions ?? [],
        version_vector: versionVector,
    };

    const hashSelf = await computeHashSelf(
        {
            event_id: eventId, aggregate_id: customer.id, aggregate_type: "customer",
            event_type: eventType, schema_version: 1, payload,
            dependencies: [], authored_at: now, authored_by: customer.organization_id,
            branch_id: branchId, org_id: customer.organization_id,
        },
        hashPrev,
    );

    return {
        event_id: eventId,
        aggregate_id: customer.id,
        aggregate_type: "customer",
        event_type: eventType,
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: now,
        authored_by: customer.organization_id,
        branch_id: branchId,
        org_id: customer.organization_id,
        hash_prev: hashPrev,
        hash_self: hashSelf,
        status: "pending",
        attempts: 0,
        error_code: null,
        error_message: null,
    };
}

async function buildStockAdjustedEnvelope(
    adjustment: StockAdjustmentCreate & { id: string; adjusted_by: string; organization_id: string },
    now: string,
    hashPrev: string,
): Promise<OutboxEvent> {
    const eventId = generateUlid();

    const payload: Record<string, unknown> = {
        branch_id: adjustment.branch_id,
        drug_id: adjustment.drug_id,
        adjustment_type: adjustment.adjustment_type,
        quantity_change: adjustment.quantity_change,
        reason: adjustment.reason ?? null,
        transfer_to_branch_id: adjustment.transfer_to_branch_id ?? null,
        adjusted_by: adjustment.adjusted_by,
    };

    const hashSelf = await computeHashSelf(
        {
            event_id: eventId, aggregate_id: adjustment.id, aggregate_type: "stock",
            event_type: "stock_adjusted", schema_version: 1, payload,
            dependencies: [], authored_at: now, authored_by: adjustment.adjusted_by,
            branch_id: adjustment.branch_id, org_id: adjustment.organization_id,
        },
        hashPrev,
    );

    return {
        event_id: eventId,
        aggregate_id: adjustment.id,
        aggregate_type: "stock",
        event_type: "stock_adjusted",
        schema_version: 1,
        payload,
        dependencies: [],
        authored_at: now,
        authored_by: adjustment.adjusted_by,
        branch_id: adjustment.branch_id,
        org_id: adjustment.organization_id,
        hash_prev: hashPrev,
        hash_self: hashSelf,
        status: "pending",
        attempts: 0,
        error_code: null,
        error_message: null,
    };
}

export function nextSyncVersion(
    current: number | undefined,
    operation: "create" | "update"
): number {
    const version = current ?? 1;
    return operation === "update" ? version + 1 : version;
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
    // Updates must advertise the next version.  The server rejects equality
    // because it means another writer may already have committed that version.
    const syncVersion = nextSyncVersion(
        record.sync_version as number | undefined,
        operation
    );
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

/** SQLite-only upsert — no sync_queue entry. Use for event-sourced tables. */
async function upsertLocal<T extends Record<string, unknown>>(
    table: string,
    record: T,
): Promise<void> {
    const db = await getDb();
    const now = new Date().toISOString();

    const payload = {
        ...record,
        sync_status: "pending",
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
    auditLog: async (
        log: {
            id?: string;
            organization_id: string;
            user_id?: string | null;
            user_full_name?: string | null;
            action: string;
            entity_type?: string | null;
            entity_id?: string | null;
            changes?: string | null;
            ip_address?: string | null;
            user_agent?: string | null;
            context_metadata?: string | null;
            created_at?: string;
            updated_at?: string;
        }
    ): Promise<void> => {
        const db = await getDb();
        const id = log.id || crypto.randomUUID();
        const now = new Date().toISOString();
        const payload = pickColumns(
            {
                ...log,
                id,
                sync_status: "synced",
                sync_version: 1,
                created_at: log.created_at || now,
                updated_at: log.updated_at || now,
            } as Record<string, unknown>,
            AUDIT_LOG_COLUMNS
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
            `INSERT INTO audit_logs (${cols.join(", ")}) VALUES (${placeholders})
             ON CONFLICT(id) DO UPDATE SET ${updates}`,
            vals
        );
    },

    sale: async (
        sale: Omit<Sale, "sync_status" | "sync_version"> & { id: string }
    ): Promise<void> => {
        const items = sale.items ?? [];
        const now = new Date().toISOString();
        const payload = buildLocalSalePayload(sale, items, now);
        await upsertLocal("sales", payload as Record<string, unknown>);

        await appendOutboxEvent((hashPrev) => buildSaleCreatedEnvelope(sale, items, now, hashPrev));
        await writeLocal.auditLog({
            organization_id: sale.organization_id,
            action: "create_sale_offline",
            entity_type: "sales",
            entity_id: sale.id,
            user_id: sale.cashier_id,
            user_full_name: sale.customer_name || "Walk-in Customer",
            changes: JSON.stringify({
                sale_number: sale.sale_number,
                total_amount: payload.total_amount,
                items_count: payload.items_count,
            }),
        });
    },

    /**
     * Add a new drug batch (received stock).
     *
     * FIX: client-computed convenience fields (`days_until_expiry`,
     * `is_expired`, `is_expiring_soon`) are destructured out — they have
     * no SQLite columns and are derived at read time.
     */
    drugBatch: async (
        batch: Omit<DrugBatch, "sync_status" | "sync_version"> & { id: string },
        operation: "create" | "update" = "create",
    ): Promise<void> => {
        const { days_until_expiry, is_expired, is_expiring_soon, ...batchData } = batch;
        await upsertAndEnqueue(
            "drug_batches",
            batchData as Record<string, unknown>,
            operation,
            { sync_protocol_version: 2 }
        );
    },

    /**
     * Create or update a branch_inventory row's branch-owned metadata
     * (existence, shelf location, branch selling price). Mirrors `drugBatch`:
     * `branch_inventory` is client-authoritative offline per the sync
     * ownership model (see syncEngine.ts header), so both a brand-new
     * branch/drug link (`addDrugToBranch`) and a metadata edit
     * (`updateBranchDrug`) upsert through the same helper — the server
     * handles create/update identically (upsert on branch_id + drug_id).
     *
     * NOTE: this is metadata only. Quantity deltas (sales, adjustments,
     * batch receipts) go through `writeLocal.inventory` instead, which
     * increments/decrements in place rather than overwriting the row.
     */
    branchDrug: async (
        branchInventory: Omit<BranchInventory, "sync_status" | "sync_version"> & { id: string },
        operation: "create" | "update" = "create",
    ): Promise<void> => {
        await upsertAndEnqueue(
            "branch_inventory",
            branchInventory as Record<string, unknown>,
            operation,
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
                    sellable_quantity: 0,
                    location: null,
                    selling_price: null,
                    updated_at: now,
                    created_at: now,
                }, "create");
            } else {
                await db.execute(
                    `INSERT INTO branch_inventory (
                       id, branch_id, drug_id, quantity, reserved_quantity, sellable_quantity,
                       location, selling_price, sync_status, sync_version,
                       synced_at, updated_at, created_at
                     ) VALUES ($1, $2, $3, $4, 0, 0, NULL, NULL, 'synced', 1, NULL, $5, $5)`,
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
        adjustment: StockAdjustmentCreate & { id: string; adjusted_by: string; organization_id: string }
    ): Promise<void> => {
        const now = new Date().toISOString();
        await appendOutboxEvent((hashPrev) => buildStockAdjustedEnvelope(adjustment, now, hashPrev));

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
                updated_at: prescription.updated_at ?? now,
                created_at: prescription.created_at ?? now,
            } as Record<string, unknown>,
            PRESCRIPTION_COLUMNS
        );

        await upsertLocal("prescriptions", payload);

        await appendOutboxEvent((hashPrev) => buildPrescriptionEnvelope(prescription, operation, now, hashPrev));
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
        operation: "create" | "update" = "create",
        branchId?: string,
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

        await upsertLocal("customers", payload);

        if (branchId) {
            await appendOutboxEvent((hashPrev) => buildCustomerEnvelope(customer, branchId, now, operation, hashPrev));
        }

        await writeLocal.auditLog({
            organization_id: customer.organization_id,
            action: `${operation}_customer_offline`,
            entity_type: "customers",
            entity_id: customer.id,
            changes: JSON.stringify({
                first_name: customer.first_name,
                last_name: customer.last_name,
                customer_type: customer.customer_type,
            }),
        });
    },
};
