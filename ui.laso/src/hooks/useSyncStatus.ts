/**
 * useSyncStatus.ts
 * ================
 * React hook that subscribes to the sync engine and exposes
 * status, pending count, last sync time, and pending conflicts
 * to any component in the app.
 */

import { useState, useEffect, useCallback } from "react";
import { syncEngine } from "@/lib/syncEngine";
import type { SyncStatus } from "@/types";
import type { QueuedConflict, QueuedFailure } from "@/lib/localDb";

export interface SyncState {
    status: SyncStatus;
    pendingCount: number;
    lastSyncAt: string | null;
    conflicts: QueuedConflict[];
    failures: QueuedFailure[];
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

export function useSyncStatus(): SyncState {
    const [status, setStatus] = useState<SyncStatus>(syncEngine.status);
    const [pendingCount, setPendingCount] = useState(0);
    const [lastSyncAt, setLastSyncAt] = useState<string | null>(syncEngine.lastSyncAt);
    const [conflicts, setConflicts] = useState<QueuedConflict[]>(syncEngine.pendingConflicts);
    const [failures, setFailures] = useState<QueuedFailure[]>(syncEngine.pendingFailures);
    useEffect(() => {
        const unsub = syncEngine.subscribe((s, count, last) => {
            setStatus(s);
            setPendingCount(count);
            setLastSyncAt(last);
            setConflicts([...syncEngine.pendingConflicts]);
            setFailures([...syncEngine.pendingFailures]);
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

    return { status, pendingCount, lastSyncAt, conflicts, failures, syncNow, resolveConflict, discardFailure, voidFailedSale };
}
