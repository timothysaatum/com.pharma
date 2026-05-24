/** * =================
 * Price contract management page.
 *
 * Features:
 *  - Paginated, filterable list
 *  - Status badges (draft / active / suspended / expired / cancelled)
 *  - Lifecycle buttons: Approve, Activate, Suspend, Duplicate, Delete
 *  - Usage metrics per contract (sales count, total discount given)
 *  - Expiry warnings (30-day alert)
 *  - Role-gating: only admin/manager/super_admin see action buttons
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
    Plus, RefreshCw, Search, Filter, X,
    CheckCircle2, AlertTriangle, Clock, Ban,
    Copy, Trash2, Edit2, ChevronLeft,
    ChevronRight, TrendingUp, 
    ShieldCheck, Zap,
} from "lucide-react";
import { contractsApi, type ContractResponse, type ContractListParams } from "@/api/contracts";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";
import { useDebounce } from "@/hooks/useDebounce";
import { ContractForm } from "@/components/contracts/ContractForm";
import { SuspendContractModal } from "@/components/contracts/SuspendContractModal";
import { DuplicateContractModal } from "@/components/contracts/DuplicateContractModal";

// ── Helpers ───────────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<string, { label: string; cls: string; icon: React.ElementType }> = {
    draft: { label: "Draft", cls: "bg-slate-100 text-slate-600", icon: Clock },
    active: { label: "Active", cls: "bg-green-50 text-green-700", icon: CheckCircle2 },
    suspended: { label: "Suspended", cls: "bg-amber-50 text-amber-700", icon: AlertTriangle },
    expired: { label: "Expired", cls: "bg-red-50 text-red-600", icon: Ban },
    cancelled: { label: "Cancelled", cls: "bg-red-100 text-red-700", icon: X },
};

const TYPE_COLORS: Record<string, string> = {
    standard: "bg-slate-100 text-slate-700",
    insurance: "bg-blue-50 text-blue-700",
    corporate: "bg-purple-50 text-purple-700",
    staff: "bg-green-50 text-green-700",
    senior_citizen: "bg-amber-50 text-amber-700",
    wholesale: "bg-orange-50 text-orange-700",
    promotional: "bg-pink-50 text-pink-700",
};

function StatusBadge({ status }: { status: string }) {
    const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.draft;
    const Icon = cfg.icon;
    return (
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${cfg.cls}`}>
            <Icon className="w-3 h-3" />
            {cfg.label}
        </span>
    );
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ContractsPage() {
    const { user } = useAuthStore();
    const canManage = !!user?.role && ["super_admin", "admin", "manager"].includes(user.role);
    const canDelete = !!user?.role && ["super_admin", "admin"].includes(user.role);

    // ── List state ─────────────────────────────────────────────────────────────
    const [contracts, setContracts] = useState<ContractResponse[]>([]);
    const [total, setTotal] = useState(0);
    const [totalPages, setTotalPages] = useState(1);
    const [page, setPage] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [searchInput, setSearchInput] = useState("");
    const debouncedSearch = useDebounce(searchInput, 300);
    const [filterType, setFilterType] = useState("");
    const [filterStatus, setFilterStatus] = useState("");
    const [statsBar, setStatsBar] = useState({ active: 0, suspended: 0, expired: 0 });

    // ── Modal state ────────────────────────────────────────────────────────────
    const [showCreate, setShowCreate] = useState(false);
    const [editingContract, setEditingContract] = useState<ContractResponse | null>(null);
    const [suspendingContract, setSuspendingContract] = useState<ContractResponse | null>(null);
    const [duplicatingContract, setDuplicatingContract] = useState<ContractResponse | null>(null);
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [approvingId, setApprovingId] = useState<string | null>(null);
    const [activatingId, setActivatingId] = useState<string | null>(null);

    const abortRef = useRef<AbortController | null>(null);

    const fetchContracts = useCallback(async () => {
        abortRef.current?.abort();
        const ctrl = new AbortController();
        abortRef.current = ctrl;

        setIsLoading(true);
        setError(null);

        const params: ContractListParams = {
            page,
            page_size: 20,
            search: debouncedSearch || undefined,
            contract_type: (filterType as ContractListParams["contract_type"]) || undefined,
            status: (filterStatus as ContractListParams["status"]) || undefined,
            sort_by: "created_at",
            sort_order: "desc",
        };

        try {
            const res = await contractsApi.list(params, ctrl.signal);
            if (!ctrl.signal.aborted) {
                setContracts(res.contracts);
                setTotal(res.total);
                setTotalPages(res.total_pages);
                setStatsBar({
                    active: res.total_active_contracts,
                    suspended: res.total_suspended_contracts,
                    expired: res.total_expired_contracts,
                });
            }
        } catch (err: unknown) {
            if (err instanceof Error && err.name === "AbortError") return;
            if (!ctrl.signal.aborted) setError(parseApiError(err));
        } finally {
            if (!ctrl.signal.aborted) setIsLoading(false);
        }
    }, [page, debouncedSearch, filterType, filterStatus]);

    useEffect(() => { fetchContracts(); return () => abortRef.current?.abort(); }, [fetchContracts]);

    // ── Actions ────────────────────────────────────────────────────────────────

    const handleApprove = async (id: string) => {
        setApprovingId(id);
        try {
            const updated = await contractsApi.approve(id);
            setContracts((prev) => prev.map((c) => c.id === id ? updated : c));
        } catch (err) { setError(parseApiError(err)); }
        finally { setApprovingId(null); }
    };

    const handleActivate = async (id: string) => {
        setActivatingId(id);
        try {
            const updated = await contractsApi.activate(id);
            setContracts((prev) => prev.map((c) => c.id === id ? updated : c));
        } catch (err) { setError(parseApiError(err)); }
        finally { setActivatingId(null); }
    };

    const handleDelete = async (id: string) => {
        if (!window.confirm("Delete this contract? This cannot be undone. Consider suspending instead.")) return;
        setDeletingId(id);
        try {
            await contractsApi.remove(id);
            setContracts((prev) => prev.filter((c) => c.id !== id));
            setTotal((t) => t - 1);
        } catch (err) { setError(parseApiError(err)); }
        finally { setDeletingId(null); }
    };

    const handleSaved = (saved: ContractResponse) => {
        setContracts((prev) => {
            const idx = prev.findIndex((c) => c.id === saved.id);
            if (idx >= 0) { const next = [...prev]; next[idx] = saved; return next; }
            return [saved, ...prev];
        });
        setShowCreate(false);
        setEditingContract(null);
    };

    const handleSuspended = (updated: ContractResponse) => {
        setContracts((prev) => prev.map((c) => c.id === updated.id ? updated : c));
        setSuspendingContract(null);
    };

    const handleDuplicated = (newContract: ContractResponse) => {
        setContracts((prev) => [newContract, ...prev]);
        setDuplicatingContract(null);
    };

    // ── Expiry helpers ─────────────────────────────────────────────────────────
    const daysUntilExpiry = (effectiveTo: string | null): number | null => {
        if (!effectiveTo) return null;
        return Math.max(0, Math.round((new Date(effectiveTo).getTime() - Date.now()) / 86400000));
    };

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex-shrink-0">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="font-display text-2xl font-bold text-ink">Price Contracts</h1>
                        <p className="text-sm text-ink-muted mt-0.5">
                            {total} contract{total !== 1 ? "s" : ""} · {statsBar.active} active
                        </p>
                    </div>
                    <div className="flex items-center gap-2">
                        <button onClick={fetchContracts} className="w-9 h-9 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors" title="Refresh">
                            <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} />
                        </button>
                        {canManage && (
                            <button onClick={() => setShowCreate(true)} className="flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors">
                                <Plus className="w-4 h-4" />New Contract
                            </button>
                        )}
                    </div>
                </div>

                {/* Stats bar */}
                <div className="flex gap-4 mt-3">
                    {[
                        { label: "Active", value: statsBar.active, cls: "text-green-600", icon: CheckCircle2 },
                        { label: "Suspended", value: statsBar.suspended, cls: "text-amber-600", icon: AlertTriangle },
                        { label: "Expired", value: statsBar.expired, cls: "text-red-500", icon: Ban },
                    ].map((s) => (
                        <div key={s.label} className="flex items-center gap-1.5 text-xs">
                            <s.icon className={`w-3.5 h-3.5 ${s.cls}`} />
                            <span className={`font-bold ${s.cls}`}>{s.value}</span>
                            <span className="text-ink-muted">{s.label}</span>
                        </div>
                    ))}
                </div>
            </div>

            {/* Filters */}
            <div className="px-6 py-3 bg-white border-b border-slate-100 flex items-center gap-3 flex-wrap flex-shrink-0">
                <div className="relative flex-1 min-w-48">
                    <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                    <input
                        value={searchInput}
                        onChange={(e) => { setSearchInput(e.target.value); setPage(1); }}
                        placeholder="Search code or name…"
                        className="w-full pl-9 pr-3 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    />
                </div>
                <select
                    value={filterType}
                    onChange={(e) => { setFilterType(e.target.value); setPage(1); }}
                    className="text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none"
                >
                    <option value="">All types</option>
                    <option value="standard">Standard</option>
                    <option value="insurance">Insurance</option>
                    <option value="corporate">Corporate</option>
                    <option value="staff">Staff</option>
                    <option value="senior_citizen">Senior Citizen</option>
                    <option value="wholesale">Wholesale</option>
                    <option value="promotional">Promotional</option>
                </select>
                <select
                    value={filterStatus}
                    onChange={(e) => { setFilterStatus(e.target.value); setPage(1); }}
                    className="text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none"
                >
                    <option value="">All statuses</option>
                    <option value="draft">Draft</option>
                    <option value="active">Active</option>
                    <option value="suspended">Suspended</option>
                    <option value="expired">Expired</option>
                    <option value="cancelled">Cancelled</option>
                </select>
                {(searchInput || filterType || filterStatus) && (
                    <button onClick={() => { setSearchInput(""); setFilterType(""); setFilterStatus(""); setPage(1); }}
                        className="flex items-center gap-1 text-xs text-ink-muted hover:text-red-500 transition-colors">
                        <X className="w-3.5 h-3.5" />Clear
                    </button>
                )}
            </div>

            {/* Error */}
            {error && (
                <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-start flex-shrink-0">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-600 flex-1">{error}</p>
                    <button onClick={() => setError(null)} className="text-ink-muted hover:text-ink"><X className="w-4 h-4" /></button>
                </div>
            )}

            {/* Table */}
            <div className="flex-1 overflow-auto px-6 py-4">
                {isLoading ? (
                    <div className="flex items-center justify-center h-48">
                        <div className="w-6 h-6 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
                    </div>
                ) : contracts.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 gap-3 text-ink-muted">
                        <Filter className="w-10 h-10 opacity-25" />
                        <p className="text-sm font-medium">No contracts found</p>
                        {canManage && <button onClick={() => setShowCreate(true)} className="text-sm text-brand-600 hover:underline">Create your first contract</button>}
                    </div>
                ) : (
                    <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="bg-slate-50 border-b border-slate-200">
                                    <th className="text-left px-5 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Contract</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Type</th>
                                    <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Discount</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Status</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Validity</th>
                                    <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Usage</th>
                                    {canManage && <th className="text-right px-5 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Actions</th>}
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100">
                                {contracts.map((c) => {
                                    const days = daysUntilExpiry(c.effective_to);
                                    const isExpiringSoon = days !== null && days <= 30 && days > 0;
                                    const isApproving = approvingId === c.id;
                                    const isActivating = activatingId === c.id;
                                    const isDeleting = deletingId === c.id;

                                    return (
                                        <motion.tr
                                            key={c.id}
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            className="hover:bg-slate-50/70 transition-colors group"
                                        >
                                            {/* Contract identity */}
                                            <td className="px-5 py-3">
                                                <div className="flex items-center gap-2">
                                                    <div>
                                                        <p className="font-semibold text-ink">
                                                            {c.contract_name}
                                                            {c.is_default_contract && (
                                                                <span className="ml-2 text-[10px] font-bold bg-brand-100 text-brand-700 px-1.5 py-0.5 rounded-full">DEFAULT</span>
                                                            )}
                                                        </p>
                                                        <p className="text-xs font-mono text-ink-muted">{c.contract_code}</p>
                                                    </div>
                                                </div>
                                            </td>
                                            {/* Type */}
                                            <td className="px-4 py-3">
                                                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full capitalize ${TYPE_COLORS[c.contract_type] ?? "bg-slate-100 text-ink-muted"}`}>
                                                    {c.contract_type.replace("_", " ")}
                                                </span>
                                            </td>
                                            {/* Discount */}
                                            <td className="px-4 py-3 text-center">
                                                <span className="font-bold text-ink">
                                                    {Number(c.discount_percentage).toFixed(1)}%
                                                </span>
                                            </td>
                                            {/* Status */}
                                            <td className="px-4 py-3">
                                                <StatusBadge status={c.status} />
                                            </td>
                                            {/* Validity */}
                                            <td className="px-4 py-3">
                                                <div className="text-xs text-ink-secondary">
                                                    <p>From {new Date(c.effective_from).toLocaleDateString()}</p>
                                                    {c.effective_to ? (
                                                        <p className={isExpiringSoon ? "text-amber-600 font-semibold" : ""}>
                                                            {isExpiringSoon && "⚠ "}
                                                            To {new Date(c.effective_to).toLocaleDateString()}
                                                            {days !== null && days <= 30 && ` (${days}d left)`}
                                                        </p>
                                                    ) : (
                                                        <p className="text-ink-muted">No expiry</p>
                                                    )}
                                                </div>
                                            </td>
                                            {/* Usage metrics */}
                                            <td className="px-4 py-3 text-right">
                                                <div className="text-xs text-ink-secondary">
                                                    <p className="flex items-center justify-end gap-1">
                                                        <TrendingUp className="w-3 h-3" />
                                                        {c.usage_count ?? 0} sales
                                                    </p>
                                                    <p className="flex items-center justify-end gap-1 text-green-600">
                                                        ₵{Number(c.total_discount_given ?? 0).toFixed(2)} given
                                                    </p>
                                                </div>
                                            </td>
                                            {/* Actions */}
                                            {canManage && (
                                                <td className="px-5 py-3">
                                                    <div className="flex items-center justify-end gap-1">
                                                        {/* Edit */}
                                                        <button
                                                            onClick={() => setEditingContract(c)}
                                                            title="Edit"
                                                            className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                                                        >
                                                            <Edit2 className="w-3.5 h-3.5" />
                                                        </button>
                                                        {/* Approve (draft only) */}
                                                        {c.status === "draft" && (
                                                            <button
                                                                onClick={() => handleApprove(c.id)}
                                                                disabled={isApproving}
                                                                title="Approve & activate"
                                                                className="w-8 h-8 flex items-center justify-center rounded-lg text-green-600 hover:bg-green-50 transition-colors disabled:opacity-50"
                                                            >
                                                                {isApproving ? <div className="w-3.5 h-3.5 border border-green-600 border-t-transparent rounded-full animate-spin" /> : <ShieldCheck className="w-3.5 h-3.5" />}
                                                            </button>
                                                        )}
                                                        {/* Activate (suspended only) */}
                                                        {c.status === "suspended" && (
                                                            <button
                                                                onClick={() => handleActivate(c.id)}
                                                                disabled={isActivating}
                                                                title="Re-activate"
                                                                className="w-8 h-8 flex items-center justify-center rounded-lg text-brand-600 hover:bg-brand-50 transition-colors disabled:opacity-50"
                                                            >
                                                                {isActivating ? <div className="w-3.5 h-3.5 border border-brand-600 border-t-transparent rounded-full animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
                                                            </button>
                                                        )}
                                                        {/* Suspend (active only) */}
                                                        {c.status === "active" && !c.is_default_contract && (
                                                            <button
                                                                onClick={() => setSuspendingContract(c)}
                                                                title="Suspend"
                                                                className="w-8 h-8 flex items-center justify-center rounded-lg text-amber-600 hover:bg-amber-50 transition-colors"
                                                            >
                                                                <AlertTriangle className="w-3.5 h-3.5" />
                                                            </button>
                                                        )}
                                                        {/* Duplicate */}
                                                        <button
                                                            onClick={() => setDuplicatingContract(c)}
                                                            title="Duplicate"
                                                            className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                                                        >
                                                            <Copy className="w-3.5 h-3.5" />
                                                        </button>
                                                        {/* Delete */}
                                                        {canDelete && !c.is_default_contract && (
                                                            <button
                                                                onClick={() => handleDelete(c.id)}
                                                                disabled={isDeleting}
                                                                title="Delete"
                                                                className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-red-500 hover:bg-red-50 transition-colors disabled:opacity-50"
                                                            >
                                                                {isDeleting ? <div className="w-3.5 h-3.5 border border-red-500 border-t-transparent rounded-full animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                                                            </button>
                                                        )}
                                                    </div>
                                                </td>
                                            )}
                                        </motion.tr>
                                    );
                                })}
                            </tbody>
                        </table>

                        {/* Pagination */}
                        {totalPages > 1 && (
                            <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-slate-50">
                                <p className="text-xs text-ink-muted">
                                    Showing {((page - 1) * 20) + 1}–{Math.min(page * 20, total)} of {total}
                                </p>
                                <div className="flex items-center gap-2">
                                    <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}
                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted hover:text-ink hover:bg-white disabled:opacity-40 transition-colors">
                                        <ChevronLeft className="w-4 h-4" />
                                    </button>
                                    <span className="text-xs text-ink-secondary font-medium">Page {page} of {totalPages}</span>
                                    <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted hover:text-ink hover:bg-white disabled:opacity-40 transition-colors">
                                        <ChevronRight className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Modals */}
            <AnimatePresence>
                {(showCreate || editingContract) && (
                    <ContractForm
                        contract={editingContract ?? undefined}
                        onSuccess={handleSaved}
                        onCancel={() => { setShowCreate(false); setEditingContract(null); }}
                    />
                )}
                {suspendingContract && (
                    <SuspendContractModal
                        contract={suspendingContract}
                        onSuccess={handleSuspended}
                        onCancel={() => setSuspendingContract(null)}
                    />
                )}
                {duplicatingContract && (
                    <DuplicateContractModal
                        contract={duplicatingContract}
                        onSuccess={handleDuplicated}
                        onCancel={() => setDuplicatingContract(null)}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}