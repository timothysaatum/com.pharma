/**
 * SyncIndicator.tsx
 * =================
 * Compact sync status widget for the AppShell sidebar.
 * Shows: idle (last sync time) | syncing (spinner) | offline | error
 * Also shows pending push count and manual-conflict badge.
 */

import { RefreshCw, WifiOff, AlertTriangle, CheckCircle2 } from "lucide-react";
import { useSyncStatus } from "@/hooks/useSyncStatus";

function formatRelative(isoString: string | null): string {
    if (!isoString) return "Never";
    const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
    if (diff < 60) return "Just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

interface SyncIndicatorProps {
    collapsed?: boolean;
}

export function SyncIndicator({ collapsed = false }: SyncIndicatorProps) {
    const { status, pendingCount, lastSyncAt, conflicts, syncNow } = useSyncStatus();

    const hasConflicts = conflicts.length > 0;

    // ── Icon and colour per status ──────────────────────────

    const icon = (() => {
        if (status === "syncing")
            return <RefreshCw className="w-3.5 h-3.5 animate-spin text-brand-400" />;
        if (status === "offline")
            return <WifiOff className="w-3.5 h-3.5 text-amber-400" />;
        if (status === "error" || hasConflicts)
            return <AlertTriangle className="w-3.5 h-3.5 text-red-400" />;
        return <CheckCircle2 className="w-3.5 h-3.5 text-brand-400" />;
    })();

    const label = (() => {
        if (status === "syncing") return "Syncing…";
        if (status === "offline") return "Offline";
        if (status === "error") return "Sync error";
        if (hasConflicts) return `${conflicts.length} conflict${conflicts.length > 1 ? "s" : ""}`;
        if (pendingCount > 0) return `${pendingCount} pending`;
        return formatRelative(lastSyncAt);
    })();

    // ── Collapsed — just show icon with tooltip ──────────────

    if (collapsed) {
        return (
            <button
                onClick={syncNow}
                title={`Sync: ${label}`}
                className="w-full flex items-center justify-center py-2 text-white/40 hover:text-white transition-colors"
            >
                {icon}
            </button>
        );
    }

    // ── Expanded ─────────────────────────────────────────────

    return (
        <div className="mx-2 mb-2 px-2.5 py-2 rounded-lg bg-white/5 border border-white/10">
            <div className="flex items-center gap-2">
                {icon}
                <span className="text-xs text-white/50 flex-1 truncate">{label}</span>

                {/* Pending badge */}
                {pendingCount > 0 && status !== "syncing" && (
                    <span className="text-xs bg-amber-500/20 text-amber-300 rounded px-1.5 py-0.5 font-mono leading-none">
                        {pendingCount}
                    </span>
                )}

                {/* Manual sync button */}
                {status !== "syncing" && status !== "offline" && (
                    <button
                        onClick={syncNow}
                        title="Sync now"
                        className="text-white/30 hover:text-white transition-colors"
                    >
                        <RefreshCw className="w-3 h-3" />
                    </button>
                )}
            </div>

            {/* Conflict warning */}
            {hasConflicts && (
                <p className="mt-1.5 text-xs text-red-400 leading-tight">
                    {conflicts.length} record{conflicts.length > 1 ? "s need" : " needs"} manual review
                </p>
            )}
        </div>
    );
}