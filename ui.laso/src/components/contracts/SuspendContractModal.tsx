/**
 * SuspendContractModal.tsx
 * ========================
 * Requires a mandatory reason before suspending.
 * The server also enforces this — we mirror it client-side.
 */
import { useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { contractsApi, type ContractResponse } from "@/api/contracts";
import { parseApiError } from "@/api/client";

export function SuspendContractModal({
    contract,
    onSuccess,
    onCancel,
}: {
    contract: ContractResponse;
    onSuccess: (updated: ContractResponse) => void;
    onCancel: () => void;
}) {
    const [reason, setReason] = useState("");
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const handleSubmit = async () => {
        if (reason.trim().length < 5) {
            setError("Please provide a reason of at least 5 characters.");
            return;
        }
        setIsSubmitting(true);
        setError(null);
        try {
            const updated = await contractsApi.suspend(contract.id, reason.trim());
            onSuccess(updated);
        } catch (err) {
            setError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm">
            <motion.div
                initial={{ opacity: 0, scale: 0.97 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.97 }}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-md"
            >
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-amber-50 flex items-center justify-center">
                            <AlertTriangle className="w-5 h-5 text-amber-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-base font-bold text-ink">Suspend Contract</h2>
                            <p className="text-xs text-ink-muted">{contract.contract_name}</p>
                        </div>
                    </div>
                    <button onClick={onCancel} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="px-6 py-4 space-y-4">
                    <p className="text-sm text-ink-secondary">
                        Suspending this contract will immediately remove it from POS selection.
                        All historical data is preserved. You can re-activate it at any time.
                    </p>
                    {error && <p className="text-sm text-red-600 bg-red-50 border border-red-100 rounded-xl px-3 py-2">{error}</p>}
                    <div>
                        <label className="block text-sm font-medium text-ink mb-1.5">
                            Reason for suspension <span className="text-red-500">*</span>
                        </label>
                        <textarea
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            rows={3}
                            placeholder="e.g. Provider contract expired, pending renewal…"
                            className="w-full px-3 py-2 rounded-xl border border-slate-200 text-sm text-ink resize-none focus:outline-none focus:ring-2 focus:ring-amber-500/30 focus:border-amber-500"
                        />
                    </div>
                    <div className="flex justify-end gap-2 pt-1">
                        <button onClick={onCancel} className="px-4 py-2 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                            Cancel
                        </button>
                        <button
                            onClick={handleSubmit}
                            disabled={isSubmitting}
                            className="px-5 py-2 text-sm font-semibold text-white bg-amber-600 hover:bg-amber-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2"
                        >
                            {isSubmitting && <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" /></svg>}
                            {isSubmitting ? "Suspending…" : "Suspend Contract"}
                        </button>
                    </div>
                </div>
            </motion.div>
        </div>
    );
}