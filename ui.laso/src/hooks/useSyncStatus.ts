/**
 * useSyncStatus.ts
 * ================
 * React hook that subscribes to the sync engine and exposes
 * status, pending count, last sync time, and pending conflicts
 * to any component in the app.
 */

import { useState, useEffect, useCallback } from "react";
import { syncEngine } from "@/lib/syncEngine";
import type { SyncHealth } from "@/lib/syncEngine";
import type { SyncStatus } from "@/types";
import type { QueuedConflict, QueuedFailure } from "@/lib/localDb";

export interface SyncState {
    status: SyncStatus;
    pendingCount: number;
    lastSyncAt: string | null;
    conflicts: QueuedConflict[];
    failures: QueuedFailure[];
    /** How far this device's event stream has got, and what it could not apply. */
    health: SyncHealth;
    /** Manually trigger a sync (e.g. from a button) */
    syncNow: () => Promise<void>;
    /** Resolve a manual conflict with server or local preference */
    resolveConflict: (
        conflict: QueuedConflict,
        resolution: "server_wins" | "local_wins"
    ) => Promise<void>;
    /** Discard a permanently failed sync record (non-sale tables only — see voidFailedSale) */
    discardFailure: (tableName: string, recordId: string) => Promise<void>;
    /** Audited, manager-approved void for a permanently failed sale */
    voidFailedSale: (failure: QueuedFailure, reason: string, approverUserId: string) => Promise<void>;
}

/** Health for a device that has not synced yet: nothing applied, nothing failed,
 *  and no known server head to compare against. */
const IDLE_HEALTH: SyncHealth = {
    pulledSeq: 0,
    serverHeadSeq: null,
    failedCount: 0,
    quarantinedCount: 0,
    stalled: false,
};

export function useSyncStatus(): SyncState {
    const [status, setStatus] = useState<SyncStatus>(syncEngine.status);
    const [pendingCount, setPendingCount] = useState(0);
    const [lastSyncAt, setLastSyncAt] = useState<string | null>(syncEngine.lastSyncAt);
    const [conflicts, setConflicts] = useState<QueuedConflict[]>(syncEngine.pendingConflicts);
    const [failures, setFailures] = useState<QueuedFailure[]>(syncEngine.pendingFailures);
    // Default to IDLE_HEALTH rather than syncEngine.syncHealth: consumers of
    // this hook routinely stub it, and reaching straight into the singleton
    // yields undefined there, which throws on first render. A consumer that
    // cares gets health from the subscription below.
    const [health, setHealth] = useState<SyncHealth>(IDLE_HEALTH);
    useEffect(() => {
        const unsub = syncEngine.subscribe((s, count, last, h) => {
            setStatus(s);
            setPendingCount(count);
            setLastSyncAt(last);
            setConflicts([...syncEngine.pendingConflicts]);
            setFailures([...syncEngine.pendingFailures]);
            // Older listeners pass only the first three arguments.
            setHealth(h ?? IDLE_HEALTH);
        });
        return unsub;
    }, []);

    const syncNow = useCallback(() => syncEngine.retryFailed(), []);

    const resolveConflict = useCallback(
        (conflict: QueuedConflict, resolution: "server_wins" | "local_wins") =>
            syncEngine.resolveConflict(conflict, resolution),
        []
    );

    const discardFailure = useCallback(
        (tableName: string, recordId: string) =>
            syncEngine.discardFailure(tableName, recordId),
        []
    );

    const voidFailedSale = useCallback(
        (failure: QueuedFailure, reason: string, approverUserId: string) =>
            syncEngine.voidFailedSale(failure, reason, approverUserId),
        []
    );

    return { status, pendingCount, lastSyncAt, conflicts, failures, health, syncNow, resolveConflict, discardFailure, voidFailedSale };
}
