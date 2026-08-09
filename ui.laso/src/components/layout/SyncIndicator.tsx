/**
 * SyncIndicator.tsx
 * =================
 * Compact sync status widget for the AppShell sidebar.
 * Shows: idle (last sync time) | syncing (spinner) | offline | error
 * Also shows pending push count and manual-conflict badge.
 */

import { useState } from "react";
import { RefreshCw, WifiOff, AlertTriangle, CheckCircle2 } from "lucide-react";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { SyncConflictModal } from "@/components/layout/SyncConflictModal";
import { VoidSaleFailureModal } from "@/components/layout/VoidSaleFailureModal";
import type { QueuedFailure } from "@/lib/localDb";

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
    const { status, pendingCount, lastSyncAt, conflicts, failures, permanentlyRejected, syncNow, discardFailure } = useSyncStatus();
    const [showConflictModal, setShowConflictModal] = useState(false);
    const [voidingFailure, setVoidingFailure] = useState<QueuedFailure | null>(null);

    const hasConflicts = conflicts.length > 0;
    const hasFailures = failures.length > 0;
    const hasPermanentlyRejected = permanentlyRejected.length > 0;
    const blockedFailures = failures.filter((failure) => failure.is_blocked).length;

    // ── Icon and colour per status ──────────────────────────

    const icon = (() => {
        if (status === "syncing")
            return <RefreshCw className="w-3.5 h-3.5 animate-spin text-brand-400" />;
        if (status === "offline")
            return <WifiOff className="w-3.5 h-3.5 text-amber-400" />;
        if (status === "error" || hasConflicts || hasFailures || hasPermanentlyRejected)
            return <AlertTriangle className="w-3.5 h-3.5 text-red-400" />;
        return <CheckCircle2 className="w-3.5 h-3.5 text-brand-400" />;
    })();

    const label = (() => {
        if (status === "syncing") return "Syncing…";
        if (status === "offline") return "Offline";
        if (hasConflicts) return `${conflicts.length} conflict${conflicts.length > 1 ? "s" : ""}`;
        if (hasFailures) {
            if (blockedFailures > 0) {
                return `${blockedFailures} blocked`;
            }
            return `${failures.length} failed`;
        }
        if (hasPermanentlyRejected) return `${permanentlyRejected.length} need${permanentlyRejected.length > 1 ? "" : "s"} review`;
        if (status === "error") return "Sync error";
        if (pendingCount > 0) return `${pendingCount} pending`;
        return formatRelative(lastSyncAt);
    })();

    // ── Collapsed — just show icon with tooltip ──────────────

    if (collapsed) {
        return (
            <>
                <button
                    onClick={() => {
                        if (hasConflicts) {
                            setShowConflictModal(true);
                        } else {
                            syncNow();
                        }
                    }}
                    title={`Sync: ${label}`}
                    className="w-full flex items-center justify-center py-2 text-white/40 hover:text-white transition-colors"
                >
                    {icon}
                </button>
                <SyncConflictModal
                    open={showConflictModal}
                    onClose={() => setShowConflictModal(false)}
                />
            </>
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
                    <span className={`text-xs rounded px-1.5 py-0.5 font-mono leading-none ${
                        hasConflicts || hasFailures || status === "error"
                            ? "bg-red-500/20 text-red-300"
                            : "bg-amber-500/20 text-amber-300"
                    }`}>
                        {pendingCount}
                    </span>
                )}

                {/* Manual sync button */}
                {status !== "syncing" && status !== "offline" && (
                    <button
                        onClick={syncNow}
                        title={hasFailures ? "Retry failed sync records" : "Sync now"}
                        className="text-white/30 hover:text-white transition-colors"
                    >
                        <RefreshCw className="w-3 h-3" />
                    </button>
                )}
            </div>

            {/* Conflict warning */}
            {hasConflicts && (
                <>
                    <p className="mt-1.5 text-xs text-red-400 leading-tight">
                        {conflicts.length} record{conflicts.length > 1 ? "s need" : " needs"} manual review
                    </p>
                    <button
                        onClick={() => setShowConflictModal(true)}
                        className="mt-2 inline-flex items-center gap-1 rounded-full border border-red-400/20 bg-red-500/10 px-3 py-1 text-xs font-semibold text-red-200 hover:bg-red-500/20 transition"
                    >
                        Resolve conflicts
                    </button>
                </>
            )}
            {hasFailures && (
                <>
                    <p className="mt-1.5 text-xs text-red-400 leading-tight">
                        {blockedFailures > 0
                            ? `${blockedFailures} record${blockedFailures > 1 ? "s have" : " has"} stopped retrying`
                            : `${failures.length} record${failures.length > 1 ? "s" : ""} failed to sync`}
                    </p>
                    <div className="mt-1 space-y-1">
                        {failures.slice(0, 3).map((failure, i) => (
                            <div key={i} className="flex items-center justify-between gap-1 text-[11px] leading-tight text-white/35 group/item">
                                <span
                                    className="truncate flex-1"
                                    title={`${failure.table_name ?? "sync"}: ${failure.error ?? "Unknown error"}${failure.is_blocked ? " (blocked)" : ""}`}
                                >
                                    {failure.table_name ?? "sync"}: {failure.error ?? "Unknown error"}
                                </span>
                                <button
                                    onClick={() =>
                                        failure.table_name === "sales"
                                            ? setVoidingFailure(failure)
                                            : discardFailure(failure.table_name, failure.record_id)
                                    }
                                    title={
                                        failure.table_name === "sales"
                                            ? "Void this sale — requires a reason and is recorded on the server"
                                            : "Discard this failed record from the sync queue"
                                    }
                                    className="text-red-400 hover:text-red-300 font-semibold px-1 rounded hover:bg-red-500/10 transition-colors ml-1 flex-shrink-0"
                                >
                                    {failure.table_name === "sales" ? "Void…" : "Discard"}
                                </button>
                            </div>
                        ))}
                        {failures.length > 3 && (
                            <p className="text-[11px] leading-tight text-white/25">
                                …and {failures.length - 3} more
                            </p>
                        )}
                    </div>
                </>
            )}
            {hasPermanentlyRejected && (
                <>
                    <p className="mt-1.5 text-xs text-red-400 leading-tight">
                        {permanentlyRejected.length} record{permanentlyRejected.length > 1 ? "s" : ""} could not be
                        recovered and {permanentlyRejected.length > 1 ? "need" : "needs"} manual review
                    </p>
                    <div className="mt-1 space-y-1">
                        {permanentlyRejected.slice(0, 3).map((row, i) => (
                            <div key={i} className="text-[11px] leading-tight text-white/35">
                                <span
                                    className="truncate block"
                                    title={`${row.table} record ${row.recordId} never reached the server and cannot be retried automatically.`}
                                >
                                    {row.table}: record never reached the server
                                </span>
                            </div>
                        ))}
                        {permanentlyRejected.length > 3 && (
                            <p className="text-[11px] leading-tight text-white/25">
                                …and {permanentlyRejected.length - 3} more
                            </p>
                        )}
                    </div>
                </>
            )}
            <SyncConflictModal
                open={showConflictModal}
                onClose={() => setShowConflictModal(false)}
            />
            <VoidSaleFailureModal
                failure={voidingFailure}
                onClose={() => setVoidingFailure(null)}
            />
        </div>
    );
}
