/**
 * ─────────────────────────────────────────────────────────────
 * Full branch management UI within SettingsPage.
 *
 * Features:
 *  - Searchable, filterable list of all org branches
 *  - Status badges (Active / Inactive) with toggle actions
 *  - "New Branch" opens CreateBranchPanel slide-over
 *  - Action error banner (activate/deactivate failures)
 *  - Empty state with prompt to create first branch
 *  - Loading skeleton
 */

import { useState, useMemo } from "react";
import {
    Plus, RefreshCw, AlertTriangle, Search,
    Building2, CheckCircle2, XCircle, Power, PowerOff,
    MapPin, Phone, Mail, Hash, Pencil,
} from "lucide-react";
import { useBranches } from "@/hooks/useBranches";
import { useBranchEdit } from "@/hooks/useBranchEdit";
import { CreateBranchPanel } from "@/components/settings/CreateBranchPanel";
import { EditBranchPanel } from "@/components/settings/EditBranchPanel";
import { useAuthStore } from "@/stores/authStore";
import type { BranchListItem, Branch } from "@/types";

// ─── Status badge ────────────────────────────────────────────

function StatusBadge({ active }: { active: boolean }) {
    return active ? (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-green-50 text-green-700 border border-green-200">
            <CheckCircle2 className="w-3 h-3" />
            Active
        </span>
    ) : (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-500">
            <XCircle className="w-3 h-3" />
            Inactive
        </span>
    );
}

// ─── Branch row ───────────────────────────────────────────────

function BranchRow({
    branch,
    onActivate,
    onDeactivate,
    onEdit,
    actionLoading,
    canManage,
}: {
    branch: BranchListItem;
    onActivate: (id: string) => void;
    onDeactivate: (id: string) => void;
    onEdit: (branch: BranchListItem) => void;
    actionLoading: boolean;
    canManage: boolean;
}) {
    return (
        <div className="px-5 py-4 hover:bg-slate-50/60 transition-colors">
            <div className="flex items-start justify-between gap-4">
                {/* Left: name + meta */}
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 mb-1">
                        <p className="text-sm font-bold text-ink truncate">{branch.name}</p>
                        <StatusBadge active={branch.is_active} />
                    </div>

                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
                        <span className="flex items-center gap-1 font-mono">
                            <Hash className="w-3 h-3" />{branch.code}
                        </span>
                        {branch.phone && (
                            <span className="flex items-center gap-1">
                                <Phone className="w-3 h-3" />{branch.phone}
                            </span>
                        )}
                        {branch.email && (
                            <span className="flex items-center gap-1">
                                <Mail className="w-3 h-3" />{branch.email}
                            </span>
                        )}
                        {branch.manager_name && (
                            <span className="flex items-center gap-1">
                                <Building2 className="w-3 h-3" />{branch.manager_name}
                            </span>
                        )}
                    </div>
                </div>

                {/* Right: actions */}
                <div className="flex items-center gap-2 flex-shrink-0">
                    {canManage && (
                        <button
                            onClick={() => onEdit(branch)}
                            title="Edit branch"
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-slate-500 border border-slate-200 rounded-xl hover:text-brand-600 hover:border-brand-200 hover:bg-brand-50 transition-colors"
                        >
                            <Pencil className="w-3.5 h-3.5" />
                            Edit
                        </button>
                    )}
                    {branch.is_active ? (
                        <button
                            onClick={() => onDeactivate(branch.id)}
                            disabled={actionLoading}
                            title="Deactivate branch"
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-slate-500 border border-slate-200 rounded-xl hover:text-red-600 hover:border-red-200 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            <PowerOff className="w-3.5 h-3.5" />
                            Deactivate
                        </button>
                    ) : (
                        <button
                            onClick={() => onActivate(branch.id)}
                            disabled={actionLoading}
                            title="Activate branch"
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-slate-500 border border-slate-200 rounded-xl hover:text-green-700 hover:border-green-200 hover:bg-green-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            <Power className="w-3.5 h-3.5" />
                            Activate
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}

// ─── Loading skeleton ─────────────────────────────────────────

function BranchSkeleton() {
    return (
        <div className="px-5 py-4 animate-pulse">
            <div className="flex items-center gap-3 mb-2">
                <div className="h-4 bg-slate-200 rounded w-40" />
                <div className="h-5 bg-slate-100 rounded-full w-14" />
            </div>
            <div className="flex gap-4">
                <div className="h-3 bg-slate-100 rounded w-20" />
                <div className="h-3 bg-slate-100 rounded w-28" />
            </div>
        </div>
    );
}

// ─── Main component ───────────────────────────────────────────

export function BranchesTab() {
    const { user } = useAuthStore();
    const {
        branches, setBranches, loading, error,
        creating, createError, clearCreateError,
        actionState,
        createBranch, activateBranch, deactivateBranch,
        refresh,
    } = useBranches();

    const [showCreate, setShowCreate] = useState(false);
    const [editingBranch, setEditingBranch] = useState<BranchListItem | null>(null);
    const [search, setSearch] = useState("");
    const [filterActive, setFilterActive] = useState<"all" | "active" | "inactive">("all");

    const { saving: editSaving, saveError: editError, clearSaveError: clearEditError, updateBranch } = useBranchEdit();

    const canManage = user?.role === "admin" || user?.role === "super_admin";

    const handleBranchSaved = (updated: Branch) => {
        // Merge updated branch back into the list
        setBranches((prev) =>
            prev.map((b) =>
                b.id === updated.id
                    ? {
                        ...b,
                        name: updated.name,
                        code: updated.code,
                        phone: updated.phone,
                        email: updated.email,
                        is_active: updated.is_active,
                        manager_name: updated.manager_name,
                    }
                    : b
            ).sort((a, b) => a.name.localeCompare(b.name))
        );
        setEditingBranch(null);
    };

    // Client-side filter — list is always small (< 200 branches per org)
    const filtered = useMemo(() => {
        return branches.filter((b) => {
            const matchesSearch =
                !search ||
                b.name.toLowerCase().includes(search.toLowerCase()) ||
                b.code.toLowerCase().includes(search.toLowerCase()) ||
                (b.manager_name ?? "").toLowerCase().includes(search.toLowerCase());

            const matchesStatus =
                filterActive === "all" ||
                (filterActive === "active" && b.is_active) ||
                (filterActive === "inactive" && !b.is_active);

            return matchesSearch && matchesStatus;
        });
    }, [branches, search, filterActive]);

    const activeBranches = branches.filter((b) => b.is_active).length;

    return (
        <div className="flex h-full min-h-0 overflow-hidden">

            {/* ── Left: list ─────────────────────────────────── */}
            <div className="flex flex-col flex-1 min-h-0 overflow-hidden">

                {/* Toolbar */}
                <div className="flex items-center gap-3 px-5 py-3 border-b border-slate-100 bg-white flex-shrink-0">
                    {/* Search */}
                    <div className="relative flex-1 max-w-xs">
                        <Search className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                        <input
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            placeholder="Search branches…"
                            className="h-9 w-full pl-9 pr-3 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                        />
                    </div>

                    {/* Status filter */}
                    <div className="flex items-center rounded-xl border border-slate-200 overflow-hidden text-xs font-semibold">
                        {(["all", "active", "inactive"] as const).map((f) => (
                            <button
                                key={f}
                                onClick={() => setFilterActive(f)}
                                className={`px-3 h-9 transition-colors capitalize ${filterActive === f
                                    ? "bg-brand-600 text-white"
                                    : "text-slate-500 hover:bg-slate-50"
                                    }`}
                            >
                                {f}
                            </button>
                        ))}
                    </div>

                    <div className="flex-1" />

                    {/* Summary */}
                    <span className="text-xs text-slate-400">
                        {activeBranches} active · {branches.length} total
                    </span>

                    {/* Refresh */}
                    <button
                        onClick={refresh}
                        disabled={loading}
                        className="p-2 rounded-xl text-slate-400 hover:text-ink hover:bg-slate-100 disabled:opacity-50 transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
                    </button>

                    {/* New Branch */}
                    {canManage && (
                        <button
                            onClick={() => {
                                clearCreateError();
                                setEditingBranch(null);
                                setShowCreate(true);
                            }}
                            className="flex items-center gap-2 px-4 py-2 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors shadow-sm"
                        >
                            <Plus className="w-4 h-4" />
                            New Branch
                        </button>
                    )}
                </div>

                {/* Action error banner */}
                {actionState.error && (
                    <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600 flex-shrink-0">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                        {actionState.error}
                    </div>
                )}

                {/* List error */}
                {error && (
                    <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600 flex-shrink-0">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                        {error}
                    </div>
                )}

                {/* Content */}
                <div className="flex-1 overflow-y-auto">
                    {loading && branches.length === 0 ? (
                        <div className="divide-y divide-slate-100">
                            {Array.from({ length: 4 }).map((_, i) => (
                                <BranchSkeleton key={i} />
                            ))}
                        </div>
                    ) : filtered.length === 0 ? (
                        <div className="flex flex-col items-center justify-center h-full gap-3 py-16 text-center">
                            <div className="w-14 h-14 rounded-2xl bg-slate-100 flex items-center justify-center">
                                <MapPin className="w-7 h-7 text-slate-300" />
                            </div>
                            <div>
                                <p className="text-sm font-semibold text-slate-500">
                                    {search || filterActive !== "all"
                                        ? "No branches match your filters"
                                        : "No branches yet"}
                                </p>
                                <p className="text-xs text-slate-400 mt-1">
                                    {!search && filterActive === "all" && canManage
                                        ? "Create your first branch using the button above"
                                        : "Try adjusting your search or filter"}
                                </p>
                            </div>
                        </div>
                    ) : (
                        <div className="divide-y divide-slate-100">
                            {filtered.map((branch) => (
                                <BranchRow
                                    key={branch.id}
                                    branch={branch}
                                    onActivate={activateBranch}
                                    onDeactivate={deactivateBranch}
                                    onEdit={(b) => { clearEditError(); setEditingBranch(b); setShowCreate(false); }}
                                    actionLoading={actionState.loading}
                                    canManage={canManage}
                                />
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* ── Right: create or edit panel ─────────────────── */}
            {(showCreate || editingBranch) && (
                <div className="w-[400px] flex-shrink-0 flex flex-col min-h-0 overflow-hidden border-l border-slate-200">
                    {showCreate ? (
                        <CreateBranchPanel
                            onSubmit={createBranch}
                            onClose={() => setShowCreate(false)}
                            submitting={creating}
                            submitError={createError}
                        />
                    ) : editingBranch ? (
                        <EditBranchPanel
                            branch={editingBranch}
                            onSaved={handleBranchSaved}
                            onClose={() => setEditingBranch(null)}
                            onUpdate={updateBranch}
                            saving={editSaving}
                            saveError={editError}
                        />
                    ) : null}
                </div>
            )}
        </div>
    );
}