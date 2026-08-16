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
    lastSync: string | null
) => void;

class SyncEngine {
    private branchId: string | null = null;
    private organizationId: string | null = null;
    private intervalId: ReturnType<typeof setInterval> | null = null;
    private retryTimeoutId: ReturnType<typeof setTimeout> | null = null;
    private listeners: StatusListener[] = [];
    private _status: SyncStatus = "idle";
    private _lastSyncAt: string | null = null;
    private _isSyncing = false;
    private networkRetryAttempt = 0;
    private _dbInitError: string | null = null;

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
            const cached = localStorage.getItem(`last_sync_at:${branchId}`);
            if (cached) this._lastSyncAt = cached;
        } catch {}

        getLastSyncAt(undefined, branchId).then((last) => {
            if (last && this.branchId === branchId) {
                this._lastSyncAt = last;
                try { localStorage.setItem(`last_sync_at:${branchId}`, last); } catch {}
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

        const timeoutId = setTimeout(() => {
            if (this._isSyncing) {
                console.warn("[SyncEngine] Sync cycle timed out after 30s — resetting syncing status.");
                this._isSyncing = false;
                this.setStatus("error");
            }
        }, 30_000);

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
            clearTimeout(timeoutId);
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

        let afterSeq = await getEventPullSeq();

        // Page through server events. Cap at 50 pages per cycle.
        for (let page = 0; page < 50; page++) {
            const response = await syncApi.pullEvents(afterSeq);

            for (const envelope of response.events) {
                let authored = false;
                try {
                    authored = await isLocallyAuthored(envelope.event_id);
                } catch {
                    // DB error during authorship check — treat as foreign and apply.
                }
                if (authored) continue;
                try {
                    await applyEventLocally(envelope);
                } catch (err) {
                    console.warn(
                        `[SyncEngine] localProjector failed for event ${envelope.event_id} (${envelope.event_type}):`,
                        err
                    );
                }
            }

            if (response.events.length > 0) {
                afterSeq = response.next_after_seq;
                await setEventPullSeq(afterSeq);
            }

            if (!response.has_more) break;
        }

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
            fn(this._status, pendingCount, lastSync ?? this._lastSyncAt);
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
