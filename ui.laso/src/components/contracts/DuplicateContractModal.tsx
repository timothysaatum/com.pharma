/**
 * DuplicateContractModal.tsx
 * ==========================
 * Clones an existing contract with a new code + name.
 * Cloned contract is always created as draft.
 * Code is validated: uppercase alphanumeric + hyphens.
 */
import { useState } from "react";
import { motion } from "framer-motion";
import { Copy, X, AlertCircle } from "lucide-react";
import { contractsApi, type ContractResponse } from "@/api/contracts";
import { parseApiError } from "@/api/client";

export function DuplicateContractModal({
    contract,
    onSuccess,
    onCancel,
}: {
    contract: ContractResponse;
    onSuccess: (newContract: ContractResponse) => void;
    onCancel: () => void;
}) {
    const [newCode, setNewCode] = useState("");
    const [newName, setNewName] = useState(`${contract.contract_name} (Copy)`);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const codeValid = /^[A-Z0-9][A-Z0-9\-]*[A-Z0-9]$/.test(newCode) || newCode.length === 1;
    const inputCls = "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500";

    const handleSubmit = async () => {
        const code = newCode.trim().toUpperCase();
        if (code.length < 2) { setError("Code must be at least 2 characters."); return; }
        if (!/^[A-Z0-9\-]+$/.test(code)) { setError("Code can only contain uppercase letters, numbers, and hyphens."); return; }
        if (code.startsWith("-") || code.endsWith("-")) { setError("Code cannot start or end with a hyphen."); return; }
        if (!newName.trim() || newName.trim().length < 3) { setError("Name must be at least 3 characters."); return; }

        setIsSubmitting(true);
        setError(null);
        try {
            const created = await contractsApi.duplicate(contract.id, code, newName.trim());
            onSuccess(created);
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
                        <div className="w-9 h-9 rounded-xl bg-brand-50 flex items-center justify-center">
                            <Copy className="w-5 h-5 text-brand-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-base font-bold text-ink">Duplicate Contract</h2>
                            <p className="text-xs text-ink-muted">Cloning: {contract.contract_code}</p>
                        </div>
                    </div>
                    <button onClick={onCancel} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="px-6 py-4 space-y-4">
                    <p className="text-sm text-ink-secondary">
                        The duplicate will be created as a <strong>draft</strong> with today as the start date.
                        Approve it when ready to use.
                    </p>
                    {error && (
                        <div className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-100 rounded-xl px-3 py-2">
                            <AlertCircle className="w-4 h-4 flex-shrink-0" />{error}
                        </div>
                    )}
                    <div>
                        <label className="block text-sm font-medium text-ink mb-1.5">
                            New Contract Code <span className="text-red-500">*</span>
                        </label>
                        <input
                            value={newCode}
                            onChange={(e) => setNewCode(e.target.value.toUpperCase())}
                            placeholder="e.g. GLICO-2025"
                            className={`${inputCls} uppercase font-mono`}
                        />
                        <p className="text-xs text-ink-muted mt-1">Uppercase letters, numbers, and hyphens only.</p>
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-ink mb-1.5">
                            New Contract Name <span className="text-red-500">*</span>
                        </label>
                        <input
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                            placeholder="e.g. GLICO Insurance 2025"
                            className={inputCls}
                        />
                    </div>
                    <div className="flex justify-end gap-2 pt-1">
                        <button onClick={onCancel} className="px-4 py-2 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                            Cancel
                        </button>
                        <button
                            onClick={handleSubmit}
                            disabled={isSubmitting}
                            className="px-5 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2"
                        >
                            {isSubmitting && <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" /></svg>}
                            {isSubmitting ? "Duplicating…" : "Duplicate Contract"}
                        </button>
                    </div>
                </div>
            </motion.div>
        </div>
    );
}