import { useState } from "react";
import { X, ShieldAlert } from "lucide-react";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { useAuthStore } from "@/stores/authStore";
import { canUser } from "@/hooks/usePermissions";
import type { QueuedFailure } from "@/lib/localDb";

interface VoidSaleFailureModalProps {
    failure: QueuedFailure | null;
    onClose: () => void;
}

/**
 * Confirmation dialog for permanently giving up on syncing a failed sale.
 *
 * This exists because a sale that failed to sync represents drugs already
 * dispensed and money already taken at the register, with no server-side
 * record of it — the old one-click "Discard" flipped a local flag and lost
 * that fact silently. Voiding now requires a reason and is recorded server
 * side under the same manager-approval gate as refunds, so it always
 * leaves an audit trail even though no Sale row was ever created.
 * See docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md (P3).
 */
export function VoidSaleFailureModal({ failure, onClose }: VoidSaleFailureModalProps) {
    const { voidFailedSale } = useSyncStatus();
    const { user } = useAuthStore();
    const [reason, setReason] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    if (!failure) return null;

    const canApprove = canUser(user, "process_refunds");
    const saleNumber =
        typeof failure.local_data?.sale_number === "string"
            ? failure.local_data.sale_number
            : failure.record_id;

    const handleConfirm = async () => {
        if (!user) return;
        if (reason.trim().length === 0) {
            setError("A reason is required to void this sale.");
            return;
        }
        setSubmitting(true);
        setError(null);
        try {
            await voidFailedSale(failure, reason.trim(), user.id);
            onClose();
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to void sale.");
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
            <div className="w-full max-w-md rounded-3xl border border-white/10 bg-slate-950 shadow-2xl overflow-hidden">
                <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-white/10">
                    <div className="flex items-center gap-2">
                        <ShieldAlert className="w-4 h-4 text-red-400" />
                        <p className="text-sm font-semibold text-white">Void unsynced sale</p>
                    </div>
                    <button
                        onClick={onClose}
                        className="rounded-full p-2 text-slate-300 hover:bg-white/10 hover:text-white transition"
                        aria-label="Close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="p-6 space-y-4">
                    <div className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-200 leading-relaxed">
                        Sale <span className="font-mono">{saleNumber}</span> was never recorded on
                        the server — it repeatedly failed to sync. Voiding stops retrying it and
                        permanently records that decision, including who approved it. This does
                        not reverse any inventory or payment already applied locally.
                    </div>

                    <div>
                        <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Reason (required)
                        </label>
                        <textarea
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            rows={3}
                            className="w-full rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-white placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-red-400/40"
                            placeholder="e.g. Reconciled manually with paper receipt #123"
                        />
                    </div>

                    {!canApprove && (
                        <p className="text-xs text-amber-300">
                            Your account may not have permission to approve this — if it fails,
                            ask a manager with refund-approval rights to void it instead.
                        </p>
                    )}

                    {error && <p className="text-xs text-red-400">{error}</p>}

                    <div className="flex justify-end gap-2 pt-1">
                        <button
                            onClick={onClose}
                            disabled={submitting}
                            className="rounded-full px-4 py-2 text-xs font-semibold text-slate-300 hover:bg-white/10 transition disabled:opacity-60"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleConfirm}
                            disabled={submitting || reason.trim().length === 0}
                            className="inline-flex items-center gap-2 rounded-full bg-red-500 px-4 py-2 text-xs font-semibold text-white transition hover:bg-red-400 disabled:cursor-wait disabled:opacity-60"
                        >
                            {submitting ? "Voiding…" : "Void sale"}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}
