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
    ensureSuppressedCrrChangesSchema,
    SYNC_QUEUE_CHANGED_EVENT,
} from "@/lib/localDb";
import {
    BACKEND_CONNECTIVITY_EVENT,
    isBackendReachable,
    isOfflineError,
} from "@/api/client";
import { RetryBackoff } from "@/lib/syncRetryBackoff";
import { SYNC_ERROR_DEPENDENCY_NOT_SYNCED } from "@/lib/syncErrorCodes";
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
    "sales",
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

function promiseWithTimeout<T>(promise: Promise<T>, timeoutMs = 15000): Promise<T> {
    return Promise.race([
        promise,
        new Promise<T>((_, reject) => {
            setTimeout(() => {
                reject(new Error(`Sync timeout after ${timeoutMs}ms`));
            }, timeoutMs);
        }),
    ]);
}

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
    private _dbInitError: string | null = null;

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
        this.loadPersistedQueueState().catch((err) => {
            console.warn("[SyncEngine] Queue change handler error:", err);
        });
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

        this._dbInitError = null;
        this.loadPersistedQueueState().catch((err) => {
            this._dbInitError = err instanceof Error ? err.message : String(err);
            console.error("[SyncEngine] Database initialization error:", this._dbInitError);
        });

        if (navigator.onLine && isBackendReachable()) {
            if (!this._dbInitError) {
                this.sync();
            } else {
                this.setStatus("error");
            }
        } else {
            this.setStatus("offline");
        }

        this.intervalId = setInterval(() => {
            if (navigator.onLine && isBackendReachable()) {
                if (!this._isSyncing) {
                    this.sync();
                }
            } else {
                this.setStatus("offline");
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
        Promise.all([
            getPendingCount(scope).catch(() => 0),
            getLastSyncAt(undefined, branchId ?? undefined).catch(() => null),
        ]).then(([count, last]) => {
            if (active && this.listeners.includes(fn)) {
                if (branchId !== this.branchId || organizationId !== this.organizationId) return;
                fn(this._status, count, last);
            }
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
        if (this._dbInitError) {
            console.warn("[SyncEngine] Sync skipped: database init failed:", this._dbInitError);
            this.setStatus("error");
            return;
        }
        this._isSyncing = true;
        this.setStatus("syncing");

        try {
            // CRR push first: prescriptions, branch_inventory, etc. must
            // land on the server before the legacy sync_queue push sends any
            // sale that references them (e.g. prescription_id FK).
            const crrPushResult = await this.pushCrr();
            // Repair a missing queue envelope before the push. This covers an
            // app restart without delaying recovery until a second sync cycle.
            await this.reconcileOfflineSales();
            const pushResult = await this.push();
            await this.pullCrr();
            if (LEGACY_SYNC_TABLES.length > 0) {
                await this.pull(pushResult.nextPullTimestamp ?? undefined);
            }
            await this.loadPersistedQueueState();
            this.networkRetryAttempt = 0;
            await this.scheduleNextQueuedRetry();

            const pending = await getPendingCount(this.queueScope());
            this.notify(pending);

            // Only count non-blocked failures for status — blocked records
            // (dead-lettered after MAX_PUSH_ATTEMPTS) show separately in the
            // indicator but should not keep the sync state in "error".
            const activeFailures = this.pendingFailures.filter((f) => !f.is_blocked);
            this.setStatus(
                pushResult.hadFailures
                    || crrPushResult?.hadFailures
                    || activeFailures.length > 0
                    ? "error"
                    : "idle"
            );
        } catch (err) {
            console.error("[SyncEngine] Sync failed:", err);
            this.logError(err, "Sync failed");
            if (isOfflineError(err)) {
                this.setStatus("offline");
                this.scheduleNetworkRetry();
            } else {
                this.setStatus("error");
                // Fatal schema/init errors are non-retryable — they persist
                // until the application is restarted.
                const msg = err instanceof Error ? err.message : String(err);
                const isSchemaError =
                    msg.includes("CR-SQLite") ||
                    msg.includes("cr-sqlite") ||
                    msg.includes("crsql_as_crr") ||
                    msg.includes("primary key") ||
                    msg.includes("NOT NULL") ||
                    msg.includes("unique index") ||
                    this._dbInitError !== null;
                if (!isSchemaError) {
                    this.scheduleNetworkRetry();
                }
            }
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
                response = await promiseWithTimeout(syncApi.push({
                    branch_id: this.branchId!,
                    records,
                }), 15000);
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
                    // Extract the server-assigned ID so markSynced can rename the local row
                    // and update dependent tables (e.g. offline_sales) to the canonical ID.
                    // Without this, the local row stays with the client UUID while the pull
                    // later upserts the server record under a different UUID — creating a
                    // duplicate customer/prescription/sale.
                    const conflictServerId = (
                        conflict.server_record as Record<string, unknown> | null
                    )?.id as string | undefined;
                    await dequeue(conflict.table_name, conflict.local_id);
                    await this.markSynced(conflict.table_name, conflict.local_id, conflictServerId);
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
                const isDeferredDependency = item.error_code === SYNC_ERROR_DEPENDENCY_NOT_SYNCED;
                const attempts = await markQueueError(item.table_name, item.local_id, error, isDeferredDependency);
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

        // Ensure customers are pulled at least once.  The shared last_sync_at
        // timestamp is the sales watermark — customers synced before the legacy
        // pull included them would never be returned by a delta pull.
        const customerLastSync = await getLastSyncAt("customers", this.branchId ?? undefined);
        if (customerLastSync === null) {
            await this.pullTableFull("customers");
        }

        let hasMore = true;
        let since: string | null = lastSyncAt;
        let totalRecords = 0;

        while (hasMore) {
            let response: PullResponse;
            try {
                console.log(`[SyncEngine] Pulling since: ${since || "initial"}`);

                const request: Partial<PullRequest> = {
                    branch_id: this.branchId!,
                    tables: [...LEGACY_SYNC_TABLES, "customers"],
                };
                if (since !== null) {
                    request.last_sync_at = since;
                }

                response = await promiseWithTimeout(syncApi.pull(request as PullRequest), 15000);
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

    private async pushCrr(): Promise<{ hadFailures: boolean }> {
        if (!this.branchId) return { hadFailures: false };
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
                if (changes.length === 0 && auditEvents.length === 0) {
                    return { hadFailures: false };
                }

                // Group by table and push in batches
                const batchSize = 500;
                const iterations = Math.max(1, Math.ceil(changes.length / batchSize));
                let hadFailures = false;
                for (let batchIndex = 0; batchIndex < iterations; batchIndex++) {
                    const i = batchIndex * batchSize;
                    const batch = changes.slice(i, i + batchSize);
                    const response = await promiseWithTimeout(syncApi.crrPush({
                        branch_id: this.branchId,
                        changes: batch as any[],
                        audit_events: batchIndex === 0 ? auditEvents : [],
                    }), 15000);

                    if (response.accepted_audit_event_ids?.length) {
                        await markCrrRenumberAuditsUploaded(
                            response.accepted_audit_event_ids,
                        );
                    }

                    if (response.total_failed > 0) {
                        hadFailures = true;
                        console.warn(
                            "[SyncEngine] CRR push: %d accepted, %d failed",
                            response.total_accepted,
                            response.total_failed,
                        );
                    }
                }

                // Only advance the global CRR cursor after a fully successful
                // batch. Advancing past a failed row would make that local
                // mutation unreachable on later retries.
                if (changes.length > 0 && !hadFailures) {
                    const maxDbVersion = Math.max(...changes.map((c) => c.db_version), 0);
                    await db.execute(
                        "INSERT INTO sync_meta(key, value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
                        [CRR_PUSH_DB_VERSION_KEY, String(maxDbVersion)]
                    );
                    await ensureSuppressedCrrChangesSchema(db);
                    await db.execute(
                        "DELETE FROM suppressed_crr_changes WHERE db_version <= $1",
                        [maxDbVersion],
                    );

                    // Bug fix: CRR push has no per-row accepted[] list, so markSynced is
                    // never called for CRR-pushed prescriptions/customers. Without this,
                    // their sync_status stays 'pending' forever, and the upsertMany guard
                    // on pull skips them, making them look perpetually un-synced locally
                    // even after the server has accepted them.
                    //
                    // `pk` here is cr-sqlite's packed-columns encoding, transported as a
                    // "b64:..." string by the Tauri IPC layer (db.rs::row_to_json) — NOT
                    // the plain row id. A prior version of this fix compared
                    // `id = String(pk)` directly, which never matched any real row (the
                    // UPDATE silently affected zero rows every time) and left prescription
                    // sync_status stuck at 'pending' forever despite successful pushes.
                    // Matching via crsql_pack_columns(id) = pk keeps the comparison at the
                    // SQL/blob level, sidestepping the need to unpack pk in JS at all.
                    const pushedTables = new Set(
                        changes
                            .filter((c) => c.table === "prescriptions" || c.table === "customers")
                            .map((c) => c.table)
                    );
                    if (pushedTables.size > 0) {
                        const crrNow = new Date().toISOString();
                        for (const table of pushedTables) {
                            try {
                                const result = await db.execute(
                                    `UPDATE ${table}
                                     SET sync_status = 'synced', synced_at = $1
                                     WHERE sync_status = 'pending'
                                       AND EXISTS (
                                         SELECT 1 FROM crsql_changes
                                         WHERE "table" = $2
                                           AND pk = crsql_pack_columns(${table}.id)
                                           AND db_version > $3 AND db_version <= $4
                                       )`,
                                    [crrNow, table, sinceDbVersion, maxDbVersion]
                                );
                                console.log(
                                    `[SyncEngine] CRR push: marked ${result.rowsAffected} ${table} row(s) as synced`
                                );
                            } catch (err) {
                                // Non-fatal: best effort; will be retried on the next sync cycle
                                console.warn(`[SyncEngine] CRR push: failed to mark ${table} rows synced`, err);
                            }
                        }
                    }
                } else if (hadFailures) {
                    console.warn(
                        "[SyncEngine] CRR push had failed rows; retaining push cursor at %d",
                        sinceDbVersion,
                    );
                }

                return { hadFailures };
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
        return { hadFailures: true };
    }

    // ── Reconcile offline sales that were never synced ──────────────

    private async reconcileOfflineSales(): Promise<void> {
        try {
            const pendingSales = await offlineSalesManager.getPendingSales();
            const db = await getDb();
            let repaired = 0;
            for (const sale of pendingSales) {
                if (sale.sync_status === "synced") continue;
                const queued = await db.select<{ record_id: string; attempts: number }[]>(
                    "SELECT record_id, attempts FROM sync_queue WHERE table_name = 'sales' AND record_id = $1 LIMIT 1",
                    [sale.id],
                );
                // A queue entry already exists for this sale — whether still
                // retrying or dead-lettered, it is not a "missing envelope."
                // Dead-lettered sales must stay dead-lettered here: resetting
                // them automatically, every sync cycle, is exactly the S3 bug
                // (docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md)
                // — a permanently-failing sale retried forever, silently,
                // because the reset ran before pendingFailures could ever
                // observe it past the dead-letter threshold. A dead-lettered
                // sale now stays visible in the UI until the user explicitly
                // retries (resetPendingFailures, via the "Sync now" button)
                // or voids it (the audited void-failed-sale flow).
                if (queued.length > 0) continue;
                // No queue entry at all — the envelope is genuinely missing
                // (e.g. the app crashed after writing the sale locally but
                // before enqueue() completed). Re-enqueue it fresh so it is
                // picked up by the next push, starting at attempts=0.
                await enqueue("sales", sale.id, "create", 1, {
                    ...sale.sale_data,
                    items: sale.sale_items,
                    sync_protocol_version: 2,
                }, sale.id);
                repaired += 1;
            }
            if (repaired > 0) {
                console.log(`[SyncEngine] Repaired ${repaired} missing offline sale queue envelope(s)`);
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
            let crrPullStallCount = 0; // tracks consecutive has_more=true with no db_version progress


            while (hasMore) {
                const requestSinceDbVersion = sinceDbVersion;
                const response = await promiseWithTimeout(syncApi.crrPull({
                    branch_id: this.branchId,
                    crr_since_db_version: sinceDbVersion,
                    customer_merge_since_version: mergeSinceVersion,
                } as PullRequest), 15000);

                // Filter out this site's own changes (already merged locally)
                const remoteChanges = (response.crr_changes ?? []).filter(
                    (c) => c.site_id !== siteId && c.table !== "sales"
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
                    crrPullStallCount++;
                    if (crrPullStallCount >= 3) {
                        throw new Error(
                            `[SyncEngine] CRR pull stalled: server reported has_more but did not advance ` +
                            `db_version after ${crrPullStallCount} consecutive attempts. ` +
                            `Aborting to prevent an infinite loop; changes will be retried on the next sync cycle.`
                        );
                    }
                    console.warn(
                        `[SyncEngine] CRR pull: has_more=true but db_version stalled (attempt ${crrPullStallCount}/3); ` +
                        "stopping to avoid a tight loop"
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
            this.logError(err, "CRR pull failed");
            throw err;
        }
    }

    // ── Full pull for a table that has never been synced ─────────────
    private async pullTableFull(table: string): Promise<void> {
        // Paginated as of the server's pull pagination fix — a branch with
        // more than one page's worth of rows (500) now returns
        // has_more=true with a resume sync_timestamp. Previously has_more
        // was never set on this path, so omitting last_sync_at here was
        // harmless (every call always terminated after one page); now it
        // must be threaded through each iteration or this loop would spin
        // forever re-fetching the same first page. See finding #5 in
        // docs/reviews/2026-08-11-offline-first-architecture-review.md.
        let hasMore = true;
        let since: string | undefined;
        while (hasMore) {
            const request: Partial<PullRequest> = {
                branch_id: this.branchId!,
                tables: [table],
                last_sync_at: since,
            };
            const response = await syncApi.pull(request as PullRequest);
            await this.applyPullResponse(response);
            await setLastSyncAt(response.sync_timestamp, table, this.branchId ?? undefined);
            hasMore = response.has_more;
            since = response.sync_timestamp;
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
            
            await db.execute("BEGIN TRANSACTION");
            try {
                for (const row of rows) {
                    const r = row as Record<string, unknown>;
                    const localId = r.id;
                    if (typeof localId === "string") {
                        // Never let a pull overwrite a row this device still has
                        // locally-pending changes for, regardless of table —
                        // purchase_orders previously only protected offline-
                        // created drafts (OFFLINE-PO- prefix), silently
                        // allowing a pull to clobber a locally-edited existing
                        // PO still awaiting sync. This path is effectively
                        // unreachable on current (CRR-migrated) installs —
                        // purchase_orders sync via the CRR pull instead — but
                        // left correct rather than as a latent trap for any
                        // future non-CRR fallback. See finding #4 (multi-device
                        // drift) in docs/reviews/2026-08-11-offline-first-architecture-review.md.
                        const existing = await db.select<{ sync_status: string }[]>(
                            `SELECT sync_status FROM ${table} WHERE id = $1 LIMIT 1`,
                            [localId]
                        );
                        const localStatus = existing[0]?.sync_status;
                        if (localStatus === "pending" || localStatus === "conflict") {
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

                    try {
                        await db.execute(
                            `INSERT INTO ${table} (${compatibleColumns.join(", ")}) VALUES (${placeholders})
                             ON CONFLICT(id) DO UPDATE SET ${updates}`,
                            vals
                        );
                    } catch (rowError: any) {
                        const msg = rowError instanceof Error ? rowError.message : String(rowError);
                        console.error(
                            `[SyncEngine] Row insertion failed in transactional upsert. ` +
                            `Table: ${table}, Primary Key: ${localId}, Operation: UPSERT, Error: ${msg}`
                        );
                        throw rowError;
                    }
                }
                await db.execute("COMMIT");
                console.log(`[SyncEngine] Transaction committed: Upserted ${rows.length} rows into ${table}`);
            } catch (err) {
                await db.execute("ROLLBACK");
                console.error(`[SyncEngine] Transaction rolled back for table ${table}. Error:`, err);
                throw err;
            }
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

        if (response.audit_logs?.length) {
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
                // Bug fix: when the server assigned a new UUID (ID rename), update
                // offline_sales.id to serverId before marking synced. Using localId
                // would hit 0 rows after the rename, leaving the sale as 'pending'
                // and causing a duplicate push on the next reconcile cycle.
                if (serverId && serverId !== localId) {
                    await db.execute(
                        `UPDATE offline_sales
                         SET id = $1, sync_status = 'synced', error_message = NULL, updated_at = $2
                         WHERE id = $3`,
                        [serverId, now, localId]
                    );
                } else {
                    await db.execute(
                        `UPDATE offline_sales
                         SET sync_status = 'synced', error_message = NULL, updated_at = $1
                         WHERE id = $2`,
                        [now, serverId ?? localId]
                    );
                }
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
        try {
            const scope = this.queueScope();
            this.pendingConflicts = await getPendingConflicts(scope);
            this.pendingFailures = await getPendingFailures(MAX_PUSH_ATTEMPTS, scope);
            this.notify(
                await getPendingCount(scope),
                await getLastSyncAt(undefined, this.branchId ?? undefined)
            );
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this._dbInitError = msg;
            this.pendingConflicts = [];
            this.pendingFailures = [];
            this.notify(0, null);
            throw err;
        }
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
        if (navigator.onLine && isBackendReachable()) {
            if (!this._isSyncing) {
                this.sync();
            }
        } else {
            this.setStatus("offline");
        }
    }

    /**
     * Audited, manager-approved replacement for discardFailure() when the
     * failed record is a sale. A sale that fails to sync represents drugs
     * already dispensed and money already taken with no server-side
     * record — silently flipping a local flag (the old discardFailure
     * behaviour) erased that fact with no trace. This requires
     * connectivity and a server-side permission check (mirrors
     * refund/cancel's manager_approval_user_id gate) before the local
     * record is dequeued. See docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
     * finding P3.
     */
    async voidFailedSale(
        failure: QueuedFailure,
        reason: string,
        approverUserId: string
    ): Promise<void> {
        if (!navigator.onLine || !isBackendReachable()) {
            throw new Error(
                "Voiding a sale requires connectivity — it must be recorded on the server."
            );
        }

        const local = failure.local_data ?? {};
        await syncApi.voidFailedSale({
            sale_id: failure.record_id,
            branch_id: String(local.branch_id ?? ""),
            reason,
            manager_approval_user_id: approverUserId,
            sale_number: typeof local.sale_number === "string" ? local.sale_number : null,
            total_amount: local.total_amount != null ? String(local.total_amount) : null,
            last_sync_error: failure.error,
            sync_attempts: failure.attempts,
        });

        await this.discardFailure(failure.table_name, failure.record_id);
    }

    async discardFailure(tableName: string, recordId: string): Promise<void> {
        const db = await getDb();
        try {
            await db.execute(
                `UPDATE ${tableName} SET sync_status = 'synced' WHERE id = $1`,
                [recordId]
            );
        } catch (err) {
            console.warn(`[SyncEngine] Failed to update sync_status to synced for ${tableName}:${recordId}:`, err);
        }

        if (tableName === "sales") {
            try {
                await db.execute(
                    `UPDATE offline_sales SET sync_status = 'synced' WHERE id = $1`,
                    [recordId]
                );
            } catch (err) {
                console.warn(`[SyncEngine] Failed to update offline_sales status for ${recordId}:`, err);
            }
        }

        await dequeue(tableName, recordId);
        await this.loadPersistedQueueState();
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
        if (this._dbInitError) {
            console.warn("[SyncEngine] Online but DB init failed:", this._dbInitError);
            this.setStatus("error");
            return;
        }
        this.sync();
    }

    private onOffline(): void {
        console.info("[SyncEngine] Gone offline");
        // A previously scheduled retry would otherwise fire while genuinely
        // offline and silently no-op (scheduleRetry re-checks navigator.onLine
        // at fire time) — cancel it outright instead of leaving a wasted
        // timer running. The 'online' event and the periodic sync interval
        // both independently resume retrying once connectivity returns, so
        // this is a cleanup, not a correctness fix on its own.
        if (this.retryTimeoutId) {
            clearTimeout(this.retryTimeoutId);
            this.retryTimeoutId = null;
        }
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
        Promise.all([
            getPendingCount(scope).catch(() => 0),
            getLastSyncAt(undefined, branchId ?? undefined).catch(() => null),
        ]).then(([count, last]) => {
            if (branchId !== this.branchId || organizationId !== this.organizationId) return;
            this.notify(count, last);
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
        const normalized =
            err instanceof Error
                ? err
                : err && typeof err === "object" && "message" in err
                    ? new Error(String((err as { message: unknown }).message))
                    : new Error(String(err));
        console.error(`[SyncEngine] ${context}:`, normalized.message);
        if (normalized.stack) {
            console.error(`[SyncEngine] ${context} stack:`, normalized.stack);
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
