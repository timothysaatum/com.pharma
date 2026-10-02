/**
 * syncEngine.ts
 * =============
 * Background sync engine that coordinates event-sourced push and pull between
 * the local SQLite database and the FastAPI backend.
 */

import { syncApi } from "@/api/sync";
import {
    getLastSyncAt,
    setLastSyncAt,
    getPendingOutboxCount,
    getPendingOutboxEvents,
    markOutboxResult,
    getEventPullSeq,
    setEventPullSeq,
    isLocallyAuthored,
    upsertPendingConflicts,
    getDb,
    recordEventProjectionFailure,
    listRetryableEventFailures,
    clearEventProjectionFailure,
    countEventProjectionFailures,
} from "@/lib/localDb";
import { applyEventLocally } from "@/lib/localProjectors";
import { conflictsApi } from "@/api/conflicts";
import {
    BACKEND_CONNECTIVITY_EVENT,
    isBackendReachable,
    isOfflineError,
} from "@/api/client";
import { RetryBackoff } from "@/lib/syncRetryBackoff";
import type { SyncStatus } from "@/types";

export type { SyncStatus } from "@/types";

type StatusListener = (
    status: SyncStatus,
    pendingCount: number,
    lastSync: string | null,
    health?: SyncHealth
) => void;

/**
 * How far this device's event stream has actually got, and what it could not
 * apply. Existed as an invisible truth before: a device whose cursor was
 * wedged behind one poison event looked identical to a healthy idle device,
 * because the only symptom was stock figures quietly disagreeing with the
 * server.
 */
export interface SyncHealth {
    /** Highest server seq this device has pulled and applied. */
    pulledSeq: number;
    /** Highest seq the server has, from the last pull. Null before the first pull. */
    serverHeadSeq: number | null;
    /** Events whose projection failed and are still being retried. */
    failedCount: number;
    /** Events that exhausted MAX_PROJECTION_ATTEMPTS and are no longer retried. */
    quarantinedCount: number;
    /** True when there is a known gap between the device and the server head. */
    stalled: boolean;
}

/**
 * Exported for tests: the singleton below is the app's instance, but the
 * cursor self-heal in `pullEvents` needs to be driven against a controlled
 * server response without starting the background timer.
 */
export class SyncEngine {
    private branchId: string | null = null;
    private organizationId: string | null = null;
    private intervalId: ReturnType<typeof setInterval> | null = null;
    private retryTimeoutId: ReturnType<typeof setTimeout> | null = null;
    private listeners: StatusListener[] = [];
    private _status: SyncStatus = "idle";
    private _lastSyncAt: string | null = (() => {
        try {
            return localStorage.getItem("last_sync_at") || null;
        } catch {
            return null;
        }
    })();
    private _isSyncing = false;
    private networkRetryAttempt = 0;
    private _dbInitError: string | null = null;
    private _pulledSeq = 0;
    private _serverHeadSeq: number | null = null;
    /**
     * Set when this device found its stored cursor sitting above the server's
     * head and rewound to 0. Kept for the current sync cycle so the reason is
     * observable (the SyncIndicator logs it and the UI can explain a sudden
     * full replay) rather than a silent repair.
     */
    private _cursorReset: {
        from: number;
        to: number;
        reason: string;
    } | null = null;
    private _syncHealth: SyncHealth = {
        pulledSeq: 0,
        serverHeadSeq: null,
        failedCount: 0,
        quarantinedCount: 0,
        stalled: false,
    };

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

    // Stubs for legacy UI compatibility
    pendingConflicts: any[] = [];
    pendingFailures: any[] = [];

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

        // Immediately restore last known sync timestamp from localStorage cache
        try {
            const cached = localStorage.getItem(`last_sync_at:${branchId}`) || localStorage.getItem("last_sync_at");
            if (cached) this._lastSyncAt = cached;
        } catch {}

        getLastSyncAt(undefined, branchId).then((last) => {
            if (last && this.branchId === branchId) {
                this._lastSyncAt = last;
                try {
                    localStorage.setItem("last_sync_at", last);
                    localStorage.setItem(`last_sync_at:${branchId}`, last);
                } catch {}
                this.notify();
            }
        }).catch(() => {});

        window.addEventListener("online", this._onOnline);
        window.addEventListener("offline", this._onOffline);
        window.addEventListener(
            BACKEND_CONNECTIVITY_EVENT,
            this._onBackendConnectivityChange,
        );

        this._dbInitError = null;

        if (navigator.onLine && isBackendReachable()) {
            this.sync();
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
        this.branchId = null;
        this.organizationId = null;
        this._status = "idle";
        // Health is per-branch: the cursor, head, and failure counts all belong
        // to the branch we just left, so carrying them over would show the next
        // branch a false "stalled" reading.
        this._pulledSeq = 0;
        this._serverHeadSeq = null;
        this._cursorReset = null;
        this._syncHealth = {
            pulledSeq: 0,
            serverHeadSeq: null,
            failedCount: 0,
            quarantinedCount: 0,
            stalled: false,
        };
        this.notify(0, this._lastSyncAt);
    }

    /** Subscribe to sync status changes. Returns an unsubscribe function. */
    subscribe(fn: StatusListener): () => void {
        let active = true;
        const branchId = this.branchId;
        this.listeners.push(fn);
        if (!this.branchId) {
            fn(this._status, 0, this._lastSyncAt);
            return () => {
                active = false;
                this.listeners = this.listeners.filter((l) => l !== fn);
            };
        }
        // Emit current known state synchronously to avoid flash of "Never synced"
        fn(this._status, 0, this._lastSyncAt);

        Promise.all([
            getPendingOutboxCount().catch(() => 0),
            getLastSyncAt(undefined, branchId ?? undefined).catch(() => this._lastSyncAt),
        ]).then(([count, last]) => {
            if (active && this.listeners.includes(fn)) {
                if (branchId !== this.branchId) return;
                if (last) this._lastSyncAt = last;
                fn(this._status, count, this._lastSyncAt);
            }
        });
        return () => {
            active = false;
            this.listeners = this.listeners.filter((l) => l !== fn);
        };
    }

    get status(): SyncStatus { return this._status; }
    get lastSyncAt(): string | null { return this._lastSyncAt; }
    get syncHealth(): SyncHealth { return this._syncHealth; }

    /**
     * Why this device rewound its cursor, or null if it did not.
     *
     * Non-null only after a pull found the stored cursor above the server's
     * head. A full replay of the org's log is expensive and surprising, so
     * callers that want to explain it to the user (or assert on it in tests)
     * read it here.
     */
    get cursorReset(): { from: number; to: number; reason: string } | null {
        return this._cursorReset;
    }

    // ── Main sync cycle: push events, then pull events ───────────────

    async sync(): Promise<void> {
        if (!this.branchId) {
            try {
                const rawBranch = localStorage.getItem("session.branch_id") || localStorage.getItem("auth.active_branch_id");
                const rawOrg = localStorage.getItem("session.organization_id") || localStorage.getItem("auth.active_organization_id");
                if (rawBranch) {
                    this.branchId = typeof rawBranch === "string" && rawBranch.startsWith('"') ? JSON.parse(rawBranch) : rawBranch;
                }
                if (rawOrg) {
                    this.organizationId = typeof rawOrg === "string" && rawOrg.startsWith('"') ? JSON.parse(rawOrg) : rawOrg;
                }
            } catch {}
        }
        if (!this.branchId || this._isSyncing) return;
        if (this._dbInitError) {
            console.warn("[SyncEngine] Sync skipped: database init failed:", this._dbInitError);
            this.setStatus("error");
            return;
        }
        this._isSyncing = true;
        this.setStatus("syncing");

        let _timeoutResolve: (() => void) | null = null;
        const _timeoutRace = new Promise<void>(res => { _timeoutResolve = res; });

        const timeoutId = setTimeout(() => {
            if (this._isSyncing) {
                console.warn("[SyncEngine] Sync cycle timed out after 30s — resetting syncing status.");
                this._isSyncing = false;
                this.setStatus("error");
            }
            _timeoutResolve?.();
        }, 30_000);

        await Promise.race([this._doSync(), _timeoutRace]);

        clearTimeout(timeoutId);
        this._isSyncing = false;
    }

    private async _doSync(): Promise<void> {
        try {
            const eventPushResult = await this.pushEvents();
            await this.pullEvents();
            const nowIso = new Date().toISOString();
            this._lastSyncAt = nowIso;
            try {
                if (this.branchId) {
                    localStorage.setItem(`last_sync_at:${this.branchId}`, nowIso);
                }
            } catch {}
            await setLastSyncAt(nowIso, undefined, this.branchId ?? undefined);
            this.networkRetryAttempt = 0;

            const pending = await getPendingOutboxCount();
            this.notify(pending, nowIso);

            this.setStatus(eventPushResult?.hadFailures ? "error" : "idle");
        } catch (err) {
            console.error("[SyncEngine] Sync failed:", err);
            this.logError(err, "Sync failed");
            if (isOfflineError(err)) {
                this.setStatus("offline");
                this.scheduleNetworkRetry();
            } else {
                this.setStatus("error");
                const msg = err instanceof Error ? err.message : String(err);
                const isSchemaError =
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
        await this.sync();
    }

    // ── EVENT-SOURCED PUSH ───────────────────────────────────────────

    private async pushEvents(): Promise<{ hadFailures: boolean }> {
        if (!this.branchId || !this.organizationId) return { hadFailures: false };

        const pending = await getPendingOutboxEvents(500);
        if (pending.length === 0) return { hadFailures: false };

        let hadFailures = false;

        // Send in batches of MAX_PUSH_BATCH (500).
        for (let offset = 0; offset < pending.length; offset += 500) {
            const batch = pending.slice(offset, offset + 500);
            let response;
            try {
                response = await syncApi.pushEvents({
                    branch_id: this.branchId,
                    client_clock: new Date().toISOString(),
                    events: batch.map((ev) => ({
                        event_id: ev.event_id,
                        aggregate_id: ev.aggregate_id,
                        aggregate_type: ev.aggregate_type as import("@/lib/eventEnvelope").AggregateType,
                        event_type: ev.event_type,
                        schema_version: ev.schema_version,
                        payload: ev.payload,
                        dependencies: ev.dependencies,
                        authored_at: ev.authored_at,
                        authored_by: ev.authored_by,
                        branch_id: ev.branch_id,
                        org_id: ev.org_id,
                        hash_self: ev.hash_self,
                        hash_prev: ev.hash_prev,
                    })),
                });
            } catch (err) {
                // Network error — mark all as failed for next-cycle retry.
                for (const ev of batch) {
                    await markOutboxResult(ev.event_id, "failed", {
                        code: "network_error",
                        message: err instanceof Error ? err.message : String(err),
                    });
                }
                hadFailures = true;
                break;
            }

            for (const result of response.results) {
                switch (result.status) {
                    case "accepted":
                        await markOutboxResult(result.event_id, "accepted");
                        break;
                    case "accepted_deferred":
                        await markOutboxResult(result.event_id, "accepted_deferred");
                        break;
                    case "rejected_permanent":
                        await markOutboxResult(result.event_id, "rejected_permanent", {
                            code: result.error_code ?? "rejected_permanent",
                            message: result.error_message ?? "",
                        });
                        hadFailures = true;
                        break;
                    case "rejected_transient":
                        await markOutboxResult(result.event_id, "failed", {
                            code: result.error_code ?? "rejected_transient",
                            message: result.error_message ?? "",
                        });
                        hadFailures = true;
                        break;
                }
            }
        }

        return { hadFailures };
    }

    // ── EVENT-SOURCED PULL ───────────────────────────────────────────

    private async pullEvents(): Promise<void> {
        if (!this.branchId) return;

        // Re-attempt previously failed projections first, while they are still
        // in reach. Each one is bounded by MAX_PROJECTION_ATTEMPTS and then
        // quarantined, so this cannot loop forever.
        await this.retryFailedProjections();

        // The cursor is per-org: seq is per-org server-side and the pull is
        // org-wide, so a single unscoped row let one org's high-water mark
        // strand the device against another.
        this._cursorReset = null;
        let afterSeq = await getEventPullSeq(this.organizationId);

        // Page through server events. Cap at 50 pages per cycle.
        for (let page = 0; page < 50; page++) {
            const response = await syncApi.pullEvents(afterSeq);

            // ── Self-heal a cursor stranded above the server head ────────
            // The server always reports its true head in `server_head_seq`. If
            // our cursor is beyond it, this device is reading from a log that
            // no longer exists (the log was reseeded, or the cursor came from
            // another environment). Reset to 0 and replay from the start.
            //
            // Without this the device received zero events forever and reported
            // itself healthy, because the server echoed our own cursor back and
            // `pulledSeq < serverHeadSeq` compared 225 < 225.
            const head = typeof response.server_head_seq === "number"
                ? response.server_head_seq
                : null;
            if (head !== null && afterSeq > head) {
                console.warn(
                    `[SyncEngine] Cursor ${afterSeq} is ahead of the server head ${head} ` +
                    `(org ${this.organizationId ?? "unknown"}). The stored position cannot exist ` +
                    `in this organisation's event log — most likely the log was reseeded or this ` +
                    `device carried a cursor from another environment. Resetting to 0 and ` +
                    `replaying the log from the start.`
                );
                this._serverHeadSeq = head;
                this._cursorReset = { from: afterSeq, to: 0, reason: "cursor_ahead_of_head" };
                await setEventPullSeq(0, this.organizationId);
                afterSeq = 0;
                // Re-pull from 0 on the next iteration of this same cycle.
                // `lastSuccessSeq` is re-derived from `afterSeq` at the top of
                // each page, so there is nothing else to reset here.
                continue;
            }
            let lastSuccessSeq = afterSeq;

            let db;
        try {
            db = await getDb();
        } catch (err) {
            this.logError(err, "Could not open local DB during pull");
            return;
        }

        for (const envelope of response.events) {
                let authored = false;
                try {
                    authored = await isLocallyAuthored(envelope.event_id);
                } catch {
                    // DB error during authorship check — treat as foreign and apply.
                }

                if (authored) {
                    if (envelope.seq != null && envelope.seq > lastSuccessSeq) {
                        lastSuccessSeq = envelope.seq;
                    }
                    continue;
                }

                try {
                    await applyEventLocally(envelope);
                    // Applied: forget any earlier failure so the table reflects
                    // current reality rather than history.
                    if (envelope.seq != null && envelope.seq > lastSuccessSeq) {
                        lastSuccessSeq = envelope.seq;
                    }
                    await clearEventProjectionFailure(db, envelope.event_id);
                } catch (err) {
                    // Record and KEEP GOING. The previous code set a flag and
                    // broke out of the page, which left the cursor pinned
                    // before the failing event and starved every later event
                    // forever: one poison event silently froze the whole device
                    // at a fixed seq with nothing logged or retryable. The
                    // failure is now durable and bounded, so advancing past it
                    // is recoverable instead of terminal.
                    console.warn(
                        `[SyncEngine] localProjector failed for event ${envelope.event_id} (${envelope.event_type}); ` +
                        `recording and continuing so later events still apply:`,
                        err
                    );
                    try {
                        await recordEventProjectionFailure(db, envelope, err);
                    } catch (recordErr) {
                        // Failing to record must not wedge the cursor either.
                        this.logError(recordErr, `Could not record failure for ${envelope.event_id}`);
                    }
                    if (envelope.seq != null && envelope.seq > lastSuccessSeq) {
                        lastSuccessSeq = envelope.seq;
                    }
                }
            }

            // Persist the cursor. Advances past failed events too, since those
            // are now tracked and retried on their own schedule.
            if (response.events.length > 0) {
                const target = response.next_after_seq > lastSuccessSeq
                    ? response.next_after_seq
                    : lastSuccessSeq;
                if (target > afterSeq) {
                    await setEventPullSeq(target, this.organizationId);
                    afterSeq = target;
                }
            }

            if (!response.has_more) {
                // Use the server's TRUE head, not next_after_seq. On an empty
                // page next_after_seq is just our own cursor echoed back, which
                // cannot distinguish "caught up" from "past my log".
                this._serverHeadSeq = typeof response.server_head_seq === "number"
                    ? response.server_head_seq
                    : response.next_after_seq;
                break;
            }
        }

        this._pulledSeq = await getEventPullSeq(this.organizationId);
        await this.refreshSyncHealth();

        // Refresh the local conflict cache so the Conflicts page works offline.
        try {
            const result = await conflictsApi.list({ status: "pending", page_size: 100 });
            await upsertPendingConflicts(
                result.conflicts.map((c) => ({
                    ...c,
                    event_id: c.event_id ?? null,
                    resolved_at: c.resolved_at ?? null,
                }))
            );
        } catch {
            // Offline or server error — local cache remains from last pull.
        }
    }

    // ── Legacy Compatibility Helpers ─────────────────────────────────

    async resolveConflict(_conflict: any, _resolution: "server_wins" | "local_wins"): Promise<void> {}
    async discardFailure(_tableName: string, _recordId: string): Promise<void> {}
    async voidFailedSale(failure: any, reason: string, approverUserId: string): Promise<void> {
        if (!navigator.onLine || !isBackendReachable()) {
            throw new Error("Voiding a sale requires connectivity.");
        }
        const local = failure?.local_data ?? {};
        await syncApi.voidFailedSale({
            sale_id: failure?.record_id ?? "",
            branch_id: String(local.branch_id ?? this.branchId ?? ""),
            reason,
            manager_approval_user_id: approverUserId,
            sale_number: typeof local.sale_number === "string" ? local.sale_number : null,
            total_amount: local.total_amount != null ? String(local.total_amount) : null,
            last_sync_error: failure?.error,
            sync_attempts: failure?.attempts ?? 0,
        });
    }

    private onOnline(): void {
        console.info("[SyncEngine] Back online — triggering sync");
        this.networkRetryAttempt = 0;
        this.sync();
    }

    private onOffline(): void {
        console.info("[SyncEngine] Gone offline");
        if (this.retryTimeoutId) {
            clearTimeout(this.retryTimeoutId);
            this.retryTimeoutId = null;
        }
        this.setStatus("offline");
    }

    private setStatus(s: SyncStatus): void {
        this._status = s;
        if (!this.branchId) {
            this.notify(0, this._lastSyncAt);
            return;
        }

        const branchId = this.branchId;
        Promise.all([
            getPendingOutboxCount().catch(() => 0),
            getLastSyncAt(undefined, branchId ?? undefined).catch(() => this._lastSyncAt),
        ]).then(([count, last]) => {
            if (branchId !== this.branchId) return;
            if (last) {
                this._lastSyncAt = last;
            }
            this.notify(count, this._lastSyncAt);
        });
    }

    private notify(pendingCount = 0, lastSync: string | null = this._lastSyncAt): void {
        for (const fn of this.listeners) {
            fn(this._status, pendingCount, lastSync ?? this._lastSyncAt, this._syncHealth);
        }
    }

    /**
     * Re-attempt projections that previously threw, newest-known seq last.
     *
     * The stored failure rows carry only identity and the error, not the
     * envelope, so a retry re-pulls the event from the server by its recorded
     * seq rather than replaying a cached copy. Events the server no longer
     * returns are dropped from the table rather than retried forever.
     *
     * Quarantined rows (attempts exhausted) are skipped: they are surfaced in
     * the UI but never retried automatically.
     */
    private async retryFailedProjections(): Promise<void> {
        let db;
        try {
            db = await getDb();
        } catch (err) {
            this.logError(err, "Could not open local DB to retry failed projections");
            return;
        }

        let retryable;
        try {
            retryable = await listRetryableEventFailures(db);
        } catch (err) {
            this.logError(err, "Could not read sync_event_failures");
            return;
        }
        if (retryable.length === 0) return;

        for (const failure of retryable) {
            try {
                // Pull the single event we previously failed on. after_seq-1 so
                // the server includes this exact seq.
                const response = await syncApi.pullEvents(Math.max(0, failure.seq - 1), 1);
                const envelope = response.events.find((e) => e.event_id === failure.event_id);
                if (!envelope) {
                    // The server no longer serves this event (retention, or it
                    // was never persisted). Stop tracking it so the table does
                    // not accumulate dead rows.
                    await clearEventProjectionFailure(db, failure.event_id);
                    continue;
                }
                await applyEventLocally(envelope);
                await clearEventProjectionFailure(db, failure.event_id);
            } catch (err) {
                // Bump attempts; recordEventProjectionFailure quarantines once
                // MAX_PROJECTION_ATTEMPTS is reached.
                try {
                    await recordEventProjectionFailure(
                        db,
                        {
                            event_id: failure.event_id,
                            seq: failure.seq,
                            event_type: failure.event_type,
                            aggregate_id: failure.aggregate_id,
                            branch_id: failure.branch_id,
                        },
                        err
                    );
                } catch (recordErr) {
                    this.logError(recordErr, `Could not re-record failure for ${failure.event_id}`);
                }
            }
        }
    }

    /** Recompute the health snapshot the UI chip renders. */
    private async refreshSyncHealth(): Promise<void> {
        let failed = 0;
        let quarantined = 0;
        try {
            const db = await getDb();
            const counts = await countEventProjectionFailures(db);
            failed = counts.failed;
            quarantined = counts.quarantined;
            this._pulledSeq = await getEventPullSeq(this.organizationId);
        } catch {
            // Leave the previous numbers rather than reporting a false zero.
            return;
        }

        this._syncHealth = {
            pulledSeq: this._pulledSeq,
            serverHeadSeq: this._serverHeadSeq,
            failedCount: failed,
            quarantinedCount: quarantined,
            stalled:
                this._serverHeadSeq !== null &&
                (failed > 0 || quarantined > 0 || this._pulledSeq < this._serverHeadSeq),
        };
    }

    private logError(err: unknown, context: string): void {
        const normalized =
            err instanceof Error
                ? err
                : err && typeof err === "object" && "message" in err
                    ? new Error(String((err as { message: unknown }).message))
                    : new Error(String(err));
        console.error(`[SyncEngine] ${context}:`, normalized.message);
    }

    private scheduleNetworkRetry(): void {
        if (!this.branchId || !navigator.onLine) return;
        const delay = new RetryBackoff().getDelay(this.networkRetryAttempt);
        this.networkRetryAttempt += 1;
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

export const syncEngine = new SyncEngine();
