/**
 * syncEngine.ts
 * =============
 * Background sync engine that coordinates pull and push between
 * the local SQLite database and the FastAPI backend.
 *
 * Ownership model:
 *   PULL-ONLY  (org-level, client never writes):
 *     drugs, drug_categories, price_contracts, customers (after dedup)
 *
 *   PUSH+PULL  (branch-level, client is source of truth offline):
 *     branch_inventory, drug_batches, stock_adjustments,
 *     sales, purchase_orders
 *
 *   SPECIAL:
 *     customers  — created locally offline, pushed to server,
 *                  conflict = manual_required (phone/email dedupe)
 */

import { syncApi } from "@/api/sync";
import {
    getDb,
    getLastSyncAt, setLastSyncAt,
    getPendingQueue, getPendingConflicts,
    dequeue, markQueueError, markQueueConflict, clearQueueConflict,
    getPendingCount,
} from "@/lib/localDb";
import { isOfflineError } from "@/api/client";
import type {
    PullRequest,
    PullResponse,
    PushRecord,
    PushResponse,
    SyncStatus,
} from "@/types";
import type { QueuedConflict } from "@/lib/localDb";

const DEFAULT_SYNC_TABLES = [
    "drugs",
    "drug_categories",
    "price_contracts",
    "customers",
    "branch_inventory",
    "drug_batches",
    "sales",
    "purchase_orders",
];

// Re-export SyncStatus so existing callers that import it from this module
// do not need to update their import paths.
export type { SyncStatus } from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// LOCAL TYPES
// ─────────────────────────────────────────────────────────────────────────────

type StatusListener = (
    status: SyncStatus,
    pendingCount: number,
    lastSync: string | null
) => void;

// ─────────────────────────────────────────────────────────────────────────────
// SYNC ENGINE
// ─────────────────────────────────────────────────────────────────────────────

class SyncEngine {
    private branchId: string | null = null;
    private intervalId: ReturnType<typeof setInterval> | null = null;
    private listeners: StatusListener[] = [];
    private _status: SyncStatus = "idle";
    private _isSyncing = false;

    // Bound references kept so addEventListener and removeEventListener
    // receive the exact same function object — arrow functions passed inline
    // create a new reference each time and can never be removed.
    private readonly _onOnline = () => this.onOnline();
    private readonly _onOffline = () => this.onOffline();

    // Pending conflict records that need manual resolution
    pendingConflicts: QueuedConflict[] = [];

    // ── Lifecycle ────────────────────────────────────────────────────

    /** Call once after login with the active branch. */
    start(branchId: string, intervalMs = 30_000): void {
        if (this.branchId === branchId && this.intervalId) {
            return;
        }
        this.stop();
        this.branchId = branchId;

        window.addEventListener("online", this._onOnline);
        window.addEventListener("offline", this._onOffline);

        void this.loadPersistedConflicts();

        if (navigator.onLine) {
            this.sync();
        } else {
            this.setStatus("offline");
        }

        this.intervalId = setInterval(() => {
            if (navigator.onLine && !this._isSyncing) {
                this.sync();
            }
        }, intervalMs);
    }

    /** Call on logout or branch switch. */
    stop(): void {
        if (this.intervalId) {
            clearInterval(this.intervalId);
            this.intervalId = null;
        }
        window.removeEventListener("online", this._onOnline);
        window.removeEventListener("offline", this._onOffline);
        this.branchId = null;
        this.setStatus("idle");
    }

    /** Subscribe to sync status changes. Returns an unsubscribe function. */
    subscribe(fn: StatusListener): () => void {
        this.listeners.push(fn);
        return () => { this.listeners = this.listeners.filter((l) => l !== fn); };
    }

    get status(): SyncStatus { return this._status; }

    // ── Main sync cycle: push first, then pull ───────────────────────

    async sync(): Promise<void> {
        if (!this.branchId || this._isSyncing) return;
        this._isSyncing = true;
        this.setStatus("syncing");

        try {
            const pushTimestamp = await this.push();
            await this.pull(pushTimestamp ?? undefined);

            const pending = await getPendingCount();
            this.notify(pending);
            this.setStatus("idle");
        } catch (err) {
            console.error("[SyncEngine] Sync failed:", err);
            this.logError(err, "Sync failed");
            if (isOfflineError(err)) {
                this.setStatus("offline");
            } else {
                this.setStatus("error");
            }
        } finally {
            this._isSyncing = false;
        }
    }

    // ── PUSH ─────────────────────────────────────────────────────────

    private async push(): Promise<string | null> {
        const queue = await getPendingQueue(500);
        if (queue.length === 0) return null;

        const records: PushRecord[] = queue.map((q) => ({
            table_name: q.table_name,
            local_id: q.record_id,
            operation: q.operation,
            sync_version: q.sync_version,
            data: JSON.parse(q.payload_json),
            created_offline_at: q.created_offline_at,
        }));

        let response: PushResponse;
        try {
            response = await syncApi.push({
                branch_id: this.branchId!,
                records,
            });
        } catch (err) {
            console.warn("[SyncEngine] Push network error:", err);
            this.logError(err, "Push network error");
            if (isOfflineError(err)) {
                this.setStatus("offline");
            }
            throw err;
        }

        // Accepted → remove from queue, mark local record as synced
        for (const item of response.accepted) {
            await dequeue(item.table_name, item.local_id);
            await this.markSynced(item.table_name, item.local_id, item.server_id);
        }

        // Conflicts
        for (const conflict of response.conflicts) {
            if (conflict.resolution === "server_wins") {
                await this.applyServerRecord(conflict.table_name, conflict.server_record);
                await dequeue(conflict.table_name, conflict.local_id);
                await this.markSynced(conflict.table_name, conflict.local_id);
            } else {
                // manual_required — persist conflict for later review
                await markQueueConflict(
                    conflict.table_name,
                    conflict.local_id,
                    "Conflict: manual resolution required",
                    conflict
                );
            }
        }

        if (response.total_conflicts > 0) {
            await this.loadPersistedConflicts();
        }

        // Failed → increment attempt counter, will retry next cycle
        for (const item of response.failed) {
            if (item.error) {
                await markQueueError(item.table_name, item.local_id, item.error);
            }
        }

        if (response.total_conflicts > 0 || response.total_failed > 0) {
            console.warn(
                `[SyncEngine] Push: ${response.total_accepted} accepted, ` +
                `${response.total_conflicts} conflicts, ${response.total_failed} failed`
            );
        }

        return response.next_pull_timestamp;
    }

    // ── PULL ─────────────────────────────────────────────────────────

    async pull(forceSince?: string): Promise<void> {
        const lastSyncAt = forceSince ?? await getLastSyncAt(undefined, this.branchId ?? undefined);

        let hasMore = true;
        let since: string | null = lastSyncAt;
        let totalRecords = 0;

        while (hasMore) {
            let response: PullResponse;
            try {
                console.log(`[SyncEngine] Pulling since: ${since || "initial"}`);

                const request: Partial<PullRequest> = {
                    branch_id: this.branchId!,
                    tables: DEFAULT_SYNC_TABLES,
                };
                if (since !== null) {
                    request.last_sync_at = since;
                }

                response = await syncApi.pull(request as PullRequest);
                console.log(
                    `[SyncEngine] Pull response: drugs=${response.drugs.length}, ` +
                    `categories=${response.drug_categories.length}, contracts=${response.price_contracts.length}, ` +
                    `customers=${response.customers.length}, inventory=${response.branch_inventory.length}, ` +
                    `batches=${response.drug_batches.length}, sales=${response.sales.length}, ` +
                    `orders=${response.purchase_orders.length}, total=${response.total_records}`
                );
            } catch (err) {
                console.warn("[SyncEngine] Pull failed:", err);
                this.logError(err, "Pull failed");
                if (isOfflineError(err)) {
                    this.setStatus("offline");
                }
                throw err;
            }

            await this.applyPullResponse(response);
            await setLastSyncAt(response.sync_timestamp, undefined, this.branchId ?? undefined);
            totalRecords += response.total_records;

            hasMore = response.has_more;
            since = response.sync_timestamp;
        }

        console.log(`[SyncEngine] Pull completed. Total records synced: ${totalRecords}`);
    }

    // ── Apply pull response to local SQLite ──────────────────────────

    private async applyPullResponse(response: PullResponse): Promise<void> {
        const db = await getDb();

        const upsertMany = async (
            table: string,
            rows: unknown[],
            columns: string[]
        ) => {
            if (rows.length === 0) return;
            
            for (const row of rows) {
                const r = row as Record<string, unknown>;
                const localId = r.id;
                if (typeof localId === "string") {
                    const existing = await db.select<{ sync_status: string }[]>(
                        `SELECT sync_status FROM ${table} WHERE id = $1 LIMIT 1`,
                        [localId]
                    );
                    const localStatus = existing[0]?.sync_status;
                    if (localStatus === "pending" || localStatus === "conflict") {
                        continue;
                    }
                }
                const vals = columns.map((c) => {
                    const v = r[c];
                    if (typeof v === "boolean") return v ? 1 : 0;
                    if (Array.isArray(v) || (typeof v === "object" && v !== null)) {
                        return JSON.stringify(v);
                    }
                    if (v === null || v === undefined) {
                        if (c === "is_deleted") return 0;
                        if (c === "is_active") return 1;
                        if (c === "insurance_verified") return 0;
                        if (c === "requires_prescription") return 0;
                        if (c === "receipt_printed") return 0;
                        if (c === "receipt_emailed") return 0;
                        if (c === "sync_version") return 1;
                        if (c === "sync_status") return "synced";
                        return null;
                    }
                    return v;
                });
                const placeholders = columns.map((_, i) => `$${i + 1}`).join(", ");
                const updates = columns
                    .filter((c) => c !== "id")
                    .map((c) => `${c} = excluded.${c}`)
                    .join(", ");

                await db.execute(
                    `INSERT INTO ${table} (${columns.join(", ")}) VALUES (${placeholders})
                     ON CONFLICT(id) DO UPDATE SET ${updates}`,
                    vals
                );
            }
            
            console.log(`[SyncEngine] Upserted ${rows.length} rows into ${table}`);
        };

        // ── Org-level (pull-only) ──────────────────────────────────────

        if (response.drugs.length) {
            await upsertMany("drugs", response.drugs, [
                "id", "organization_id", "name", "generic_name", "brand_name",
                "sku", "barcode", "category_id", "drug_type", "dosage_form",
                "strength", "manufacturer", "supplier",
                "requires_prescription", "controlled_substance_schedule", "ndc_code",
                "unit_price", "cost_price", "markup_percentage", "tax_rate",
                "reorder_level", "reorder_quantity", "max_stock_level",
                "unit_of_measure", "description", "usage_instructions",
                "side_effects", "contraindications", "storage_conditions",
                "is_active", "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.drug_categories.length) {
            await upsertMany("drug_categories", response.drug_categories, [
                "id", "organization_id", "name", "description", "parent_id",
                "path", "level", "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.price_contracts.length) {
            await upsertMany("price_contracts", response.price_contracts, [
                "id", "organization_id", "contract_code", "contract_name",
                "contract_type", "is_default_contract", "discount_type",
                "discount_percentage", "applies_to_prescription_only",
                "applies_to_otc", "applies_to_all_branches",
                "applicable_branch_ids", "effective_from", "effective_to",
                "status", "is_active", "copay_amount", "copay_percentage",
                "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.customers.length) {
            await upsertMany("customers", response.customers, [
                "id", "organization_id", "customer_type", "first_name",
                "last_name", "phone", "email", "date_of_birth",
                "loyalty_points", "loyalty_tier", "insurance_provider_id",
                "insurance_member_id", "preferred_contract_id",
                "is_active", "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        // ── Branch-level ───────────────────────────────────────────────

        if (response.branch_inventory.length) {
            await upsertMany("branch_inventory", response.branch_inventory, [
                "id", "branch_id", "drug_id", "quantity", "reserved_quantity",
                "location", "selling_price", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.drug_batches.length) {
            await upsertMany("drug_batches", response.drug_batches, [
                "id", "branch_id", "drug_id", "batch_number", "quantity",
                "remaining_quantity", "manufacturing_date", "expiry_date",
                "cost_price", "selling_price", "supplier", "purchase_order_id",
                "sync_status", "sync_version", "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.sales.length) {
            await upsertMany("sales", response.sales, [
                "id", "organization_id", "branch_id", "sale_number",
                "customer_id", "customer_name",
                // financials — aligned to rewritten Sale model
                "subtotal", "discount_amount", "tax_amount", "total_amount",
                // contract snapshot
                "price_contract_id", "contract_name", "contract_discount_percentage",
                // payment
                "payment_method", "payment_status", "amount_paid",
                "change_amount", "payment_reference",
                // prescription
                "prescription_id", "prescription_number",
                "prescriber_name",
                // staff
                "cashier_id", "pharmacist_id",
                // insurance
                "insurance_claim_number",
                "patient_copay_amount", "insurance_covered_amount",
                "insurance_verified", "insurance_verified_at", "insurance_verified_by",
                // status & audit
                "notes", "status",
                "cancelled_at", "cancelled_by", "cancellation_reason",
                "refund_amount", "refunded_at",
                // receipt
                "receipt_printed", "receipt_emailed",
                "items_count",
                // sync
                "sync_status", "sync_version", "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.purchase_orders.length) {
            await upsertMany("purchase_orders", response.purchase_orders, [
                "id", "organization_id", "branch_id", "po_number",
                "supplier_id", "subtotal", "tax_amount", "shipping_cost",
                "total_amount", "status", "ordered_by", "approved_by",
                "approved_at", "expected_delivery_date", "received_date",
                "notes",
                // items_json intentionally omitted — PurchaseOrderResponse has no
                // such field; items are written locally via localWrite.purchaseOrder.
                "sync_status", "sync_version", "synced_at", "updated_at", "created_at",
            ]);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────

    private async markSynced(
        table: string,
        localId: string,
        _serverId?: string
    ): Promise<void> {
        const db = await getDb();
        const now = new Date().toISOString();
        try {
            await db.execute(
                `UPDATE ${table} SET sync_status = 'synced', synced_at = $1 WHERE id = $2`,
                [now, localId]
            );
        } catch {
            // Table may not have sync_status (e.g. sync_queue) — safe to ignore
        }
    }

    private async loadPersistedConflicts(): Promise<void> {
        this.pendingConflicts = await getPendingConflicts();
        this.notify(
            await getPendingCount(),
            await getLastSyncAt(undefined, this.branchId ?? undefined)
        );
    }

    async resolveConflict(
        conflict: QueuedConflict,
        resolution: "server_wins" | "local_wins"
    ): Promise<void> {
        const serverId = conflict.conflict.server_record?.id as string | undefined;

        if (resolution === "server_wins") {
            if (conflict.table_name === "customers") {
                const db = await getDb();
                await db.execute(
                    "DELETE FROM customers WHERE id = $1",
                    [conflict.record_id]
                );
                if (serverId) {
                    await this.applyServerRecord(conflict.table_name, conflict.conflict.server_record);
                    await db.execute(
                        "UPDATE sales SET customer_id = $1 WHERE customer_id = $2",
                        [serverId, conflict.record_id]
                    );
                }
            } else {
                await this.applyServerRecord(conflict.table_name, conflict.conflict.server_record);
            }
            await dequeue(conflict.table_name, conflict.record_id);
            await clearQueueConflict(conflict.table_name, conflict.record_id);
        } else {
            await clearQueueConflict(conflict.table_name, conflict.record_id);
        }

        await this.loadPersistedConflicts();
        if (navigator.onLine && !this._isSyncing) {
            this.sync();
        }
    }

    private async applyServerRecord(
        table: string,
        serverRecord: Record<string, unknown>
    ): Promise<void> {
        const base: Record<string, unknown> = {
            drugs: [], drug_categories: [], price_contracts: [],
            customers: [], branch_inventory: [], drug_batches: [],
            sales: [], purchase_orders: [],
            sync_timestamp: new Date().toISOString(),
            has_more: false,
            total_records: 1,
        };
        base[table] = [serverRecord];
        await this.applyPullResponse(base as unknown as PullResponse);
    }

    private onOnline(): void {
        console.info("[SyncEngine] Back online — triggering sync");
        this.sync();
    }

    private onOffline(): void {
        console.info("[SyncEngine] Gone offline");
        this.setStatus("offline");
    }

    private setStatus(s: SyncStatus): void {
        this._status = s;
        getPendingCount().then((count) => {
            getLastSyncAt(undefined, this.branchId ?? undefined).then((last) => this.notify(count, last));
        });
    }

    private notify(pendingCount = 0, lastSync: string | null = null): void {
        for (const fn of this.listeners) {
            fn(this._status, pendingCount, lastSync);
        }
    }

    private logError(err: unknown, context: string): void {
        try {
            const serialized = JSON.stringify(err, Object.getOwnPropertyNames(err), 2);
            console.error(`[SyncEngine] ${context} details:`, serialized);
        } catch (_) {
            console.error(`[SyncEngine] ${context} details (non-serializable error):`, err);
        }
    }
}

// Singleton — one engine for the lifetime of the app session
export const syncEngine = new SyncEngine();
