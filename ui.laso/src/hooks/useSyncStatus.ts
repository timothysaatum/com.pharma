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
import type { QueuedConflict } from "@/lib/localDb";

export interface SyncState {
    status: SyncStatus;
    pendingCount: number;
    lastSyncAt: string | null;
    conflicts: QueuedConflict[];
    /** Manually trigger a sync (e.g. from a button) */
    syncNow: () => Promise<void>;
    /** Resolve a manual conflict with server or local preference */
    resolveConflict: (
        conflict: QueuedConflict,
        resolution: "server_wins" | "local_wins"
    ) => Promise<void>;
}

export function useSyncStatus(): SyncState {
    const [status, setStatus] = useState<SyncStatus>(syncEngine.status);
    const [pendingCount, setPendingCount] = useState(0);
    const [lastSyncAt, setLastSyncAt] = useState<string | null>(null);
    const [conflicts, setConflicts] = useState<QueuedConflict[]>(syncEngine.pendingConflicts);

    useEffect(() => {
        const unsub = syncEngine.subscribe((s, count, last) => {
            setStatus(s);
            setPendingCount(count);
            setLastSyncAt(last);
            setConflicts([...syncEngine.pendingConflicts]);
        });
        return unsub;
    }, []);

    const syncNow = useCallback(() => syncEngine.sync(), []);

    const resolveConflict = useCallback(
        (conflict: QueuedConflict, resolution: "server_wins" | "local_wins") =>
            syncEngine.resolveConflict(conflict, resolution),
        []
    );

    return { status, pendingCount, lastSyncAt, conflicts, syncNow, resolveConflict };
}