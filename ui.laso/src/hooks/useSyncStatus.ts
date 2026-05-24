/**
 * useSyncStatus.ts
 * ================
 * React hook that subscribes to the sync engine and exposes
 * status, pending count, last sync time, and pending conflicts
 * to any component in the app.
 */

import { useState, useEffect } from "react";
import { syncEngine } from "@/lib/syncEngine";
import type { SyncStatus, PushConflict } from "@/types";

export interface SyncState {
    status: SyncStatus;
    pendingCount: number;
    lastSyncAt: string | null;
    conflicts: PushConflict[];
    /** Manually trigger a sync (e.g. from a button) */
    syncNow: () => Promise<void>;
    /** Dismiss a resolved conflict from the list */
    dismissConflict: (localId: string) => void;
}

export function useSyncStatus(): SyncState {
    const [status, setStatus] = useState<SyncStatus>(syncEngine.status);
    const [pendingCount, setPendingCount] = useState(0);
    const [lastSyncAt, setLastSyncAt] = useState<string | null>(null);
    const [conflicts, setConflicts] = useState<PushConflict[]>(syncEngine.pendingConflicts);

    useEffect(() => {
        const unsub = syncEngine.subscribe((s, count, last) => {
            setStatus(s);
            setPendingCount(count);
            setLastSyncAt(last);
            setConflicts([...syncEngine.pendingConflicts]);
        });
        return unsub;
    }, []);

    const syncNow = () => syncEngine.sync();

    const dismissConflict = (localId: string) => {
        syncEngine.pendingConflicts = syncEngine.pendingConflicts.filter(
            (c) => c.local_id !== localId
        );
        setConflicts([...syncEngine.pendingConflicts]);
    };

    return { status, pendingCount, lastSyncAt, conflicts, syncNow, dismissConflict };
}