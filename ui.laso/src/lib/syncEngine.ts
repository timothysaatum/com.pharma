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
 *     branch_inventory, drug_batches, sales, purchase_orders
 *
 *   PUSH-ONLY:
 *     stock_adjustments (immutable audit commands)
 *
 *   SPECIAL:
 *     customers  — created locally offline, pushed to server,
 *                  conflict = manual_required (phone/email dedupe)
 */

import { syncApi } from "@/api/sync";
import {
    getDb,
    getLastSyncAt, setLastSyncAt,
    getPendingQueue, getPendingConflicts, getPendingFailures, resetPendingFailures,
    dequeue, markQueueError, markQueueConflict,
    getPendingCount, getNextRetryAt, requeueConflictForLocalWin,
    getCrrPushChanges, applyCrrPullChanges, getCrrSiteId,
    applyCustomerMergeDirectives,
    getPendingCrrRenumberAudits, markCrrRenumberAuditsUploaded,
    isCrrTable, enqueue,
    SYNC_QUEUE_CHANGED_EVENT,
} from "@/lib/localDb";
import {
    BACKEND_CONNECTIVITY_EVENT,
    isBackendReachable,
    isOfflineError,
} from "@/api/client";
import { RetryBackoff } from "@/lib/syncRetryBackoff";
import { offlineSalesManager } from "@/lib/offlineSalesManager";
import type {
    PullRequest,
    PullResponse,
    PushRecord,
    PushResponse,
    SyncStatus,
} from "@/types";
import type { QueueScope, QueuedConflict, QueuedFailure } from "@/lib/localDb";

export const LEGACY_SYNC_TABLES = [
];

export async function getCompatibleLocalColumns(
    db: { select<T>(sql: string, values?: any[]): Promise<T> },
    table: string,
    requested: string[],
): Promise<string[]> {
    const info = await db.select<Array<{ name: string }>>(
        `PRAGMA table_info(${table})`
    );
    if (!info.length) return requested;
    const available = new Set(info.map((column) => column.name));
    return requested.filter((column) => available.has(column));
}

/** Maximum push attempts before an item is dead-lettered (excluded from sync queue). */
const MAX_PUSH_ATTEMPTS = 10;
const LEGACY_CRR_DB_VERSION_KEY = "crr_db_version";
const CRR_PUSH_DB_VERSION_KEY = "crr_push_db_version";
const CRR_PULL_DB_VERSION_KEY = "crr_pull_db_version";
const CUSTOMER_MERGE_VERSION_KEY = "customer_merge_directive_version";

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
    private organizationId: string | null = null;
    private intervalId: ReturnType<typeof setInterval> | null = null;
    private retryTimeoutId: ReturnType<typeof setTimeout> | null = null;
    private listeners: StatusListener[] = [];
    private _status: SyncStatus = "idle";
    private _isSyncing = false;
    private networkRetryAttempt = 0;

    // Bound references kept so addEventListener and removeEventListener
    // receive the exact same function object — arrow functions passed inline
    // create a new reference each time and can never be removed.
    private readonly _onOnline = () => this.onOnline();
    private readonly _onOffline = () => this.onOffline();
    private readonly _onBackendConnectivityChange = (event: Event) => {
        const detail = (event as CustomEvent<{ reachable?: boolean }>).detail;
        if (detail?.reachable === false) {
            this.onOffline();
            return;
        }
        if (detail?.reachable === true) {
            this.onOnline();
        }
    };
    private readonly _onQueueChanged = () => {
        void this.loadPersistedQueueState();
    };

    // Pending conflict records that need manual resolution
    pendingConflicts: QueuedConflict[] = [];
    pendingFailures: QueuedFailure[] = [];

    // ── Lifecycle ────────────────────────────────────────────────────

    /** Call once after login with the active branch and organization. */
    start(
        branchId: string,
        organizationIdOrIntervalMs?: string | number | null,
        intervalMs = 30_000
    ): void {
        const organizationId =
            typeof organizationIdOrIntervalMs === "number"
                ? null
                : organizationIdOrIntervalMs ?? null;
        const effectiveIntervalMs =
            typeof organizationIdOrIntervalMs === "number"
                ? organizationIdOrIntervalMs
                : intervalMs;

        if (
            this.branchId === branchId
            && this.organizationId === organizationId
            && this.intervalId
        ) {
            return;
        }
        this.stop();
        this.branchId = branchId;
        this.organizationId = organizationId;

        window.addEventListener("online", this._onOnline);
        window.addEventListener("offline", this._onOffline);
        window.addEventListener(
            BACKEND_CONNECTIVITY_EVENT,
            this._onBackendConnectivityChange,
        );
        window.addEventListener(SYNC_QUEUE_CHANGED_EVENT, this._onQueueChanged);

        void this.loadPersistedQueueState();

        if (navigator.onLine && isBackendReachable()) {
            this.sync();
        } else {
            this.setStatus("offline");
        }

        this.intervalId = setInterval(() => {
            if (navigator.onLine && isBackendReachable() && !this._isSyncing) {
                this.sync();
            }
        }, effectiveIntervalMs);
    }

    /** Call on logout or branch switch. */
    stop(): void {
        if (this.intervalId) {
            clearInterval(this.intervalId);
            this.intervalId = null;
        }
        if (this.retryTimeoutId) {
            clearTimeout(this.retryTimeoutId);
            this.retryTimeoutId = null;
        }
        window.removeEventListener("online", this._onOnline);
        window.removeEventListener("offline", this._onOffline);
        window.removeEventListener(
            BACKEND_CONNECTIVITY_EVENT,
            this._onBackendConnectivityChange,
        );
        window.removeEventListener(SYNC_QUEUE_CHANGED_EVENT, this._onQueueChanged);
        this.branchId = null;
        this.organizationId = null;
        this.pendingConflicts = [];
        this.pendingFailures = [];
        this._status = "idle";
        this.notify(0, null);
    }

    /** Subscribe to sync status changes. Returns an unsubscribe function. */
    subscribe(fn: StatusListener): () => void {
        let active = true;
        const branchId = this.branchId;
        const organizationId = this.organizationId;
        const scope = this.queueScope();
        this.listeners.push(fn);
        if (!this.hasActiveQueueScope()) {
            fn(this._status, 0, null);
            return () => {
                active = false;
                this.listeners = this.listeners.filter((l) => l !== fn);
            };
        }
        void getPendingCount(scope).then((count) => {
            getLastSyncAt(undefined, branchId ?? undefined).then((last) => {
                if (active && this.listeners.includes(fn)) {
                    if (branchId !== this.branchId || organizationId !== this.organizationId) return;
                    fn(this._status, count, last);
                }
            });
        });
        return () => {
            active = false;
            this.listeners = this.listeners.filter((l) => l !== fn);
        };
    }

    get status(): SyncStatus { return this._status; }

    // ── Main sync cycle: push first, then pull ───────────────────────

    async sync(): Promise<void> {
        if (!this.branchId || this._isSyncing) return;
        this._isSyncing = true;
        this.setStatus("syncing");

        try {
            const pushResult = await this.push();
            await this.pushCrr();
            await this.reconcileOfflineSales();
            await this.pullCrr();
            if (LEGACY_SYNC_TABLES.length > 0) {
                await this.pull(pushResult.nextPullTimestamp ?? undefined);
            }
            await this.loadPersistedQueueState();
            this.networkRetryAttempt = 0;
            await this.scheduleNextQueuedRetry();

            const pending = await getPendingCount(this.queueScope());
            this.notify(pending);
            this.setStatus(
                pushResult.hadFailures || this.pendingFailures.length > 0
                    ? "error"
                    : "idle"
            );
        } catch (err) {
            console.error("[SyncEngine] Sync failed:", err);
            this.logError(err, "Sync failed");
            if (isOfflineError(err)) {
                this.setStatus("offline");
            } else {
                this.setStatus("error");
            }
            this.scheduleNetworkRetry();
        } finally {
            this._isSyncing = false;
        }
    }

    async retryFailed(): Promise<void> {
        if (!this.branchId || this._isSyncing) return;
        await resetPendingFailures(this.queueScope());
        await this.loadPersistedQueueState();
        await this.sync();
    }

    // ── PUSH ─────────────────────────────────────────────────────────

    private async push(): Promise<{ nextPullTimestamp: string | null; hadFailures: boolean }> {
        let lastPullTimestamp: string | null = null;
        let hadFailures = false;
        let hasMore = true;
        const batchSize = 100;

        while (hasMore) {
            const queue = await getPendingQueue(batchSize, MAX_PUSH_ATTEMPTS, this.queueScope());
            if (queue.length === 0) {
                hasMore = false;
                break;
            }

            const records: PushRecord[] = [];
            for (const queued of queue) {
                try {
                    const data = JSON.parse(
                        queued.payload_json
                    ) as Record<string, unknown>;
                    if (!data || typeof data !== "object" || Array.isArray(data)) {
                        throw new TypeError("Queue payload must be an object.");
                    }
                    records.push({
                        operation_id: queued.operation_id,
                        table_name: queued.table_name,
                        local_id: queued.record_id,
                        operation: queued.operation,
                        sync_version: queued.sync_version,
                        data,
                        force: data._force_sync_overwrite === true,
                        created_offline_at: queued.created_offline_at,
                    });
                } catch (error) {
                    hadFailures = true;
                    await markQueueError(
                        queued.table_name,
                        queued.record_id,
                        `Invalid local queue payload: ${
                            error instanceof Error ? error.message : String(error)
                        }`
                    );
                }
            }
            if (records.length === 0) {
                hasMore = queue.length === batchSize;
                continue;
            }

            let response: PushResponse;
            try {
                response = await syncApi.push({
                    branch_id: this.branchId!,
                    records,
                });
                lastPullTimestamp = response.next_pull_timestamp;
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
                await this.markSynced(item.table_name, item.local_id, item.server_id);
                await dequeue(item.table_name, item.local_id);
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
                await this.loadPersistedQueueState();
            }

            // Failed → increment attempt counter; items exceeding MAX_PUSH_ATTEMPTS
            // are dead-lettered (excluded by getPendingQueue's attempts < MAX_PUSH_ATTEMPTS)
            let deadLettered = 0;
            for (const item of response.failed) {
                hadFailures = true;
                const error = item.error?.trim() || "Server rejected the record without an error message.";
                const attempts = await markQueueError(item.table_name, item.local_id, error);
                if (attempts >= MAX_PUSH_ATTEMPTS) {
                    deadLettered++;
                }
            }

            const handledKeys = new Set([
                ...response.accepted.map(
                    (item) => `${item.table_name}:${item.local_id}`
                ),
                ...response.conflicts.map(
                    (item) => `${item.table_name}:${item.local_id}`
                ),
                ...response.failed.map(
                    (item) => `${item.table_name}:${item.local_id}`
                ),
            ]);
            for (const record of records) {
                const key = `${record.table_name}:${record.local_id}`;
                if (!handledKeys.has(key)) {
                    hadFailures = true;
                    await markQueueError(
                        record.table_name,
                        record.local_id,
                        "Sync server omitted this record from its response."
                    );
                }
            }
            if (deadLettered > 0) {
                console.warn(
                    `[SyncEngine] ${deadLettered} item(s) exceeded max retries ` +
                    `(${MAX_PUSH_ATTEMPTS}) and will be skipped until re-edited`
                );
            }

            if (response.total_conflicts > 0 || response.total_failed > 0) {
                await this.loadPersistedQueueState();
                console.warn(
                    `[SyncEngine] Push: ${response.total_accepted} accepted, ` +
                    `${response.total_conflicts} conflicts, ${response.total_failed} failed`
                );
            }

            // If we pushed fewer than batchSize, we're likely done with the current queue
            if (queue.length < batchSize) {
                hasMore = false;
            }
        }

        return { nextPullTimestamp: lastPullTimestamp, hadFailures };
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
                    tables: LEGACY_SYNC_TABLES,
                };
                if (since !== null) {
                    request.last_sync_at = since;
                }

                response = await syncApi.pull(request as PullRequest);
                console.log(
                    `[SyncEngine] Pull response: drugs=${response.drugs.length}, ` +
                    `categories=${response.drug_categories.length}, contracts=${response.price_contracts.length}, ` +
                    `customers=${response.customers.length}, prescriptions=${response.prescriptions.length}, ` +
                    `inventory=${response.branch_inventory.length}, ` +
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

    // ── PUSH CRR (cr-sqlite changes) ────────────────────────────────

    private async pushCrr(): Promise<void> {
        if (!this.branchId) return;
        const MAX_RETRIES = 3;
        for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
            try {
                const db = await getDb();
                const rows = await db.select<{ key: string; value: string }[]>(
                    "SELECT key, value FROM sync_meta WHERE key IN ($1, $2)",
                    [CRR_PUSH_DB_VERSION_KEY, LEGACY_CRR_DB_VERSION_KEY]
                );
                const byKey = new Map(rows.map((row) => [row.key, row.value]));
                const sinceDbVersion = parseInt(
                    byKey.get(CRR_PUSH_DB_VERSION_KEY)
                    ?? byKey.get(LEGACY_CRR_DB_VERSION_KEY)
                    ?? "0",
                    10,
                );

                const changes = await getCrrPushChanges(sinceDbVersion);
                const auditEvents = await getPendingCrrRenumberAudits();
                if (changes.length === 0 && auditEvents.length === 0) return;

                // Group by table and push in batches
                const batchSize = 500;
                const iterations = Math.max(1, Math.ceil(changes.length / batchSize));
                for (let batchIndex = 0; batchIndex < iterations; batchIndex++) {
                    const i = batchIndex * batchSize;
                    const batch = changes.slice(i, i + batchSize);
                    const response = await syncApi.crrPush({
                        branch_id: this.branchId,
                        changes: batch as any[],
                        audit_events: batchIndex === 0 ? auditEvents : [],
                    });

                    if (response.accepted_audit_event_ids?.length) {
                        await markCrrRenumberAuditsUploaded(
                            response.accepted_audit_event_ids,
                        );
                    }

                    if (response.total_failed > 0) {
                        console.warn(
                            "[SyncEngine] CRR push: %d accepted, %d failed",
                            response.total_accepted,
                            response.total_failed,
                        );
                    }
                }

                // Update last pushed db_version
                if (changes.length > 0) {
                    const maxDbVersion = Math.max(...changes.map((c) => c.db_version), 0);
                    await db.execute(
                        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
                        [CRR_PUSH_DB_VERSION_KEY, String(maxDbVersion)]
                    );
                }

                return;
            } catch (err) {
                this.logError(err, `CRR push attempt ${attempt + 1}/${MAX_RETRIES}`);
                if (attempt < MAX_RETRIES - 1) {
                    const backoff = new RetryBackoff();
                    const delayMs = backoff.getDelay(attempt);
                    await new Promise((r) => setTimeout(r, delayMs));
                } else {
                    console.warn(
                        `[SyncEngine] CRR push failed after ${MAX_RETRIES} attempts; ` +
                        "leaving crsql_changes pending for the next CRR retry"
                    );
                }
            }
        }
    }

    // ── Reconcile offline sales that were never synced ──────────────

    private async reconcileOfflineSales(): Promise<void> {
        try {
            const pendingSales = await offlineSalesManager.getPendingSales();
            for (const sale of pendingSales) {
                if (sale.sync_status !== "synced") {
                    await enqueue("sales", sale.id, "create", 1, sale.sale_data as Record<string, unknown>);
                }
            }
            if (pendingSales.length > 0) {
                console.log(`[SyncEngine] Re-queued ${pendingSales.length} offline sales for sync`);
            }
        } catch (err) {
            this.logError(err, "Reconcile offline sales");
        }
    }

    // ── PULL CRR (cr-sqlite changes) ────────────────────────────────

    private async pullCrr(): Promise<void> {
        if (!this.branchId) return;
        try {
            const db = await getDb();
            const rows = await db.select<{ key: string; value: string }[]>(
                "SELECT key, value FROM sync_meta WHERE key IN ($1, $2)",
                [CRR_PULL_DB_VERSION_KEY, LEGACY_CRR_DB_VERSION_KEY]
            );
            const byKey = new Map(rows.map((row) => [row.key, row.value]));
            let sinceDbVersion = parseInt(
                byKey.get(CRR_PULL_DB_VERSION_KEY)
                ?? byKey.get(LEGACY_CRR_DB_VERSION_KEY)
                ?? "0",
                10,
            );
            const mergeRows = await db.select<{ value: string }[]>(
                "SELECT value FROM sync_meta WHERE key = $1",
                [CUSTOMER_MERGE_VERSION_KEY]
            );
            let mergeSinceVersion = parseInt(mergeRows?.[0]?.value ?? "0", 10);
            const siteId = await getCrrSiteId();
            let totalRemoteChanges = 0;
            let totalMergeDirectives = 0;
            let hasMore = true;

            while (hasMore) {
                const requestSinceDbVersion = sinceDbVersion;
                const response = await syncApi.crrPull({
                    branch_id: this.branchId,
                    crr_since_db_version: sinceDbVersion,
                    customer_merge_since_version: mergeSinceVersion,
                } as PullRequest);

                // Filter out this site's own changes (already merged locally)
                const remoteChanges = (response.crr_changes ?? []).filter(
                    (c) => c.site_id !== siteId
                );
                if (remoteChanges.length > 0) {
                    await applyCrrPullChanges(remoteChanges);
                    totalRemoteChanges += remoteChanges.length;
                }
                const mergeDirectives = response.customer_merge_directives ?? [];
                if (mergeDirectives.length > 0) {
                    await applyCustomerMergeDirectives(mergeDirectives);
                    totalMergeDirectives += mergeDirectives.length;
                }

                // Update last pulled db_version without advancing the local push cursor.
                const newDbVersion = response.crr_max_db_version ?? 0;
                if (newDbVersion > sinceDbVersion) {
                    sinceDbVersion = newDbVersion;
                    await db.execute(
                        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
                        [CRR_PULL_DB_VERSION_KEY, String(newDbVersion)]
                    );
                }
                const newMergeVersion = response.customer_merge_max_version ?? mergeSinceVersion;
                if (newMergeVersion > mergeSinceVersion) {
                    mergeSinceVersion = newMergeVersion;
                    await db.execute(
                        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
                        [CUSTOMER_MERGE_VERSION_KEY, String(newMergeVersion)]
                    );
                }

                hasMore = response.has_more && newDbVersion > requestSinceDbVersion;
                if (response.has_more && !hasMore) {
                    console.warn(
                        "[SyncEngine] CRR pull reported has_more without cursor progress; stopping to avoid a tight loop"
                    );
                }
            }

            console.log(
                "[SyncEngine] CRR pull: %d changes, %d customer merges applied (db_version → %d)",
                totalRemoteChanges,
                totalMergeDirectives,
                sinceDbVersion,
            );
        } catch (err) {
            console.warn("[SyncEngine] CRR pull error:", err);
        }
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
            const compatibleColumns = await getCompatibleLocalColumns(
                db, table, columns
            );
            
            for (const row of rows) {
                const r = row as Record<string, unknown>;
                const localId = r.id;
                if (typeof localId === "string") {
                    const existing = table === "purchase_orders"
                        ? await db.select<{ sync_status: string; po_number?: string }[]>(
                            "SELECT sync_status, po_number FROM purchase_orders WHERE id = $1 LIMIT 1",
                            [localId]
                        )
                        : await db.select<{ sync_status: string; po_number?: string }[]>(
                            `SELECT sync_status FROM ${table} WHERE id = $1 LIMIT 1`,
                            [localId]
                        );
                    const localStatus = existing[0]?.sync_status;
                    const isProtectedOfflinePurchaseOrder =
                        table === "purchase_orders"
                        && (existing[0]?.po_number?.startsWith("OFFLINE-PO-") ?? false);
                    if (
                        (localStatus === "pending" || localStatus === "conflict")
                        && (table !== "purchase_orders" || isProtectedOfflinePurchaseOrder)
                    ) {
                        continue;
                    }
                }
                const vals = compatibleColumns.map((c) => {
                    const v = r[c];
                    if (typeof v === "boolean") return v ? 1 : 0;
                    if (Array.isArray(v) || (typeof v === "object" && v !== null)) {
                        return JSON.stringify(v);
                    }
                    if (v === null || v === undefined) {
                        if (c === "is_deleted") return 0;
                        if (c === "is_active") return 1;
                        if (c === "discount_amount") return 0;
                        if (c === "tax_amount") return 0;
                        if (c === "subtotal") return 0;
                        if (c === "total_amount") return 0;
                        if (c === "insurance_verified") return 0;
                        if (c === "requires_prescription") return 0;
                        if (c === "requires_verification") return 0;
                        if (c === "requires_approval") return 0;
                        if (c === "requires_preauthorization") return 0;
                        if (c === "receipt_printed") return 0;
                        if (c === "receipt_emailed") return 0;
                        if (c === "status") return "completed";
                        if (c === "payment_status") return "completed";
                        if (c === "payment_method") return "cash";
                        if (c === "sync_version") return 1;
                        if (c === "sync_status") return "synced";
                        return null;
                    }
                    return v;
                });
                const placeholders = compatibleColumns.map((_, i) => `$${i + 1}`).join(", ");
                const updates = compatibleColumns
                    .filter((c) => c !== "id")
                    .map((c) => `${c} = excluded.${c}`)
                    .join(", ");

                await db.execute(
                    `INSERT INTO ${table} (${compatibleColumns.join(", ")}) VALUES (${placeholders})
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
                "requires_verification", "requires_approval",
                "daily_usage_limit", "per_customer_usage_limit",
                "insurance_provider_id", "requires_preauthorization",
                "minimum_purchase_amount", "maximum_purchase_amount",
                "status", "is_active", "copay_amount", "copay_percentage",
                "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.customers.length && !(await isCrrTable("customers"))) {
            await upsertMany("customers", response.customers, [
                "id", "organization_id", "customer_type", "first_name",
                "last_name", "phone", "email", "date_of_birth",
                "loyalty_points", "loyalty_tier", "insurance_provider_id",
                "insurance_member_id", "preferred_contract_id",
                "is_active", "is_deleted", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.prescriptions.length && !(await isCrrTable("prescriptions"))) {
            await upsertMany("prescriptions", response.prescriptions, [
                "id", "organization_id", "branch_id", "prescription_number",
                "customer_id", "prescriber_name", "prescriber_license",
                "prescriber_phone", "prescriber_address", "issue_date",
                "expiry_date", "medications", "diagnosis", "notes",
                "special_instructions", "refills_allowed", "refills_remaining",
                "last_refill_date", "status", "verified_by", "verified_at",
                "sync_status", "sync_version", "synced_at", "updated_at",
                "created_at",
            ]);
        }

        // ── Branch-level ───────────────────────────────────────────────
        // NOTE: branch_inventory is now a CRR table managed by cr-sqlite.
        // The old pull response for it is stale — skip it here. CRR changes
        // are applied via pullCrr() → applyCrrPullChanges() which triggers
        // cr-sqlite's automatic merge.

        if (response.branch_inventory.length && !(await isCrrTable("branch_inventory"))) {
            await upsertMany("branch_inventory", response.branch_inventory, [
                "id", "branch_id", "drug_id", "quantity", "reserved_quantity",
                "location", "selling_price", "sync_status", "sync_version",
                "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.drug_batches.length && !(await isCrrTable("drug_batches"))) {
            await upsertMany("drug_batches", response.drug_batches, [
                "id", "branch_id", "drug_id", "batch_number", "quantity",
                "remaining_quantity", "manufacturing_date", "expiry_date",
                "cost_price", "selling_price", "supplier", "purchase_order_id",
                "sync_status", "sync_version", "synced_at", "updated_at", "created_at",
            ]);
        }

        if (response.sales.length && !(await isCrrTable("sales"))) {
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

        if (response.purchase_orders.length && !(await isCrrTable("purchase_orders"))) {
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

        if (response.audit_logs.length) {
            await upsertMany("audit_logs", response.audit_logs, [
                "id", "organization_id", "user_id", "user_full_name", "action",
                "entity_type", "entity_id", "changes", "ip_address", "user_agent",
                "context_metadata", "created_at", "updated_at",
                "sync_status", "sync_version", "last_synced_at", "sync_hash",
            ]);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────

    private async markSynced(
        table: string,
        localId: string,
        serverId?: string
    ): Promise<void> {
        const db = await getDb();
        const now = new Date().toISOString();
        try {
            if (serverId && serverId !== localId) {
                const serverRows = await db.select<{ id: string }[]>(
                    `SELECT id FROM ${table} WHERE id = $1 LIMIT 1`,
                    [serverId]
                );
                if (serverRows.length > 0) {
                    await db.execute(`DELETE FROM ${table} WHERE id = $1`, [localId]);
                } else {
                    await db.execute(
                        `UPDATE ${table} SET id = $1 WHERE id = $2`,
                        [serverId, localId]
                    );
                }
            }

            await db.execute(
                `UPDATE ${table} SET sync_status = 'synced', synced_at = $1 WHERE id = $2`,
                [now, serverId ?? localId]
            );

            if (table === "sales") {
                await db.execute(
                    `UPDATE offline_sales
                     SET sync_status = 'synced', error_message = NULL, updated_at = $1
                     WHERE id = $2`,
                    [now, localId]
                );
            }
        } catch {
            // Table may not have sync_status (e.g. sync_queue) — safe to ignore
        }
    }

    private async loadPersistedQueueState(): Promise<void> {
        if (!this.hasActiveQueueScope()) {
            this.pendingConflicts = [];
            this.pendingFailures = [];
            this.notify(0, null);
            return;
        }
        const scope = this.queueScope();
        this.pendingConflicts = await getPendingConflicts(scope);
        this.pendingFailures = await getPendingFailures(MAX_PUSH_ATTEMPTS, scope);
        this.notify(
            await getPendingCount(scope),
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
                if (serverId && serverId !== conflict.record_id) {
                    const db = await getDb();
                    await db.execute(
                        `DELETE FROM ${conflict.table_name} WHERE id = $1`,
                        [conflict.record_id]
                    );
                }
                await this.applyServerRecord(conflict.table_name, conflict.conflict.server_record);
            }
            await dequeue(conflict.table_name, conflict.record_id);
        } else {
            await requeueConflictForLocalWin(conflict);
        }

        await this.loadPersistedQueueState();
        if (navigator.onLine && isBackendReachable() && !this._isSyncing) {
            this.sync();
        }
    }

    private async applyServerRecord(
        table: string,
        serverRecord: Record<string, unknown>
    ): Promise<void> {
        const base: Record<string, unknown> = {
            drugs: [], drug_categories: [], price_contracts: [],
            customers: [], prescriptions: [], branch_inventory: [], drug_batches: [],
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
        this.networkRetryAttempt = 0;
        this.sync();
    }

    private onOffline(): void {
        console.info("[SyncEngine] Gone offline");
        this.setStatus("offline");
    }

    private setStatus(s: SyncStatus): void {
        this._status = s;
        if (!this.hasActiveQueueScope()) {
            this.notify(0, null);
            return;
        }

        const branchId = this.branchId;
        const organizationId = this.organizationId;
        const scope = this.queueScope();
        getPendingCount(scope).then((count) => {
            getLastSyncAt(undefined, branchId ?? undefined).then((last) => {
                if (branchId !== this.branchId || organizationId !== this.organizationId) return;
                this.notify(count, last);
            });
        });
    }

    private hasActiveQueueScope(): boolean {
        return Boolean(this.organizationId || this.branchId);
    }

    private queueScope(): QueueScope {
        return {
            organizationId: this.organizationId ?? undefined,
            branchId: this.branchId ?? undefined,
        };
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

    private scheduleNetworkRetry(): void {
        if (!this.branchId || !navigator.onLine) return;
        const delay = new RetryBackoff().getDelay(this.networkRetryAttempt);
        this.networkRetryAttempt += 1;
        this.scheduleRetry(delay);
    }

    private async scheduleNextQueuedRetry(): Promise<void> {
        if (!this.branchId) return;
        const nextRetryAt = await getNextRetryAt(this.queueScope());
        if (!nextRetryAt) return;
        const delay = Math.max(0, new Date(nextRetryAt).getTime() - Date.now());
        this.scheduleRetry(delay);
    }

    private scheduleRetry(delayMs: number): void {
        if (this.retryTimeoutId) {
            clearTimeout(this.retryTimeoutId);
        }
        this.retryTimeoutId = setTimeout(() => {
            this.retryTimeoutId = null;
            if (navigator.onLine && !this._isSyncing) {
                void this.sync();
            }
        }, delayMs);
    }
}

// Singleton — one engine for the lifetime of the app session
export const syncEngine = new SyncEngine();
