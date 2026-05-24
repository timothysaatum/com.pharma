import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
    Search, Plus, Filter, ChevronLeft, ChevronRight,
    Pill, AlertTriangle, Package, Edit2, ToggleLeft,
    ToggleRight, RefreshCw, X, FolderTree,
} from "lucide-react";
import { drugApi } from "@/api/drugs";
import { useAuthStore } from "@/stores/authStore";
import { useDebounce } from "@/hooks/useDebounce";
import { useCategoryTree } from "@/hooks/useCategories";
import { DrugForm } from "@/components/drugs/DrugForm";
import { AddBatchForm } from "@/components/inventory/AddBatchForm";
import { DrugCategoryModal } from "@/components/drugs/DrugCategoryModal";
import { parseApiError } from "@/api/client";
import { appEvents, useAppEvent } from "@/lib/events";
import type { Drug, DrugBatch, DrugType, DrugCategoryTree } from "@/types";

// ── Render hierarchical <option> elements from the category tree ─────────────
// Matches the same helper in DrugForm so the two dropdowns stay in sync.
function renderCategoryOptions(
    nodes: DrugCategoryTree[],
    depth = 0
): React.ReactNode[] {
    return nodes.flatMap((node) => [
        <option key={node.id} value={node.id}>
            {"\u00A0\u00A0".repeat(depth * 2)}{depth > 0 ? "↳ " : ""}{node.name}
        </option>,
        ...renderCategoryOptions(node.children ?? [], depth + 1),
    ]);
}

// ── Flatten a category tree into a Map<id, name> for O(1) table row lookup ──
function flattenTree(
    nodes: DrugCategoryTree[],
    acc = new Map<string, string>()
): Map<string, string> {
    for (const node of nodes) {
        acc.set(node.id, node.name);
        if (node.children?.length) flattenTree(node.children, acc);
    }
    return acc;
}

const DRUG_TYPE_LABELS: Record<string, string> = {
    otc: "OTC",
    prescription: "Rx",
    controlled: "Controlled",
    herbal: "Herbal",
    supplement: "Supplement",
};

const DRUG_TYPE_COLORS: Record<string, string> = {
    otc: "bg-blue-50 text-blue-700 border-blue-100",
    prescription: "bg-purple-50 text-purple-700 border-purple-100",
    controlled: "bg-red-50 text-red-700 border-red-100",
    herbal: "bg-green-50 text-green-700 border-green-100",
    supplement: "bg-amber-50 text-amber-700 border-amber-100",
};

export default function DrugListPage() {
    const { user, activeBranchId } = useAuthStore();
    const canEdit = !!user?.role && ["admin", "manager", "super_admin"].includes(user.role);

    // FIX: tree gives hierarchy in the filter dropdown — shared cache with DrugForm
    const { tree: categoryTree, invalidate: invalidateCategories } = useCategoryTree();
    // FIX: flat id→name map for O(1) table row lookups
    const categoryMap = flattenTree(categoryTree);

    // ── State ─────────────────────────────────────────────────
    const [drugs, setDrugs] = useState<Drug[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Search input is debounced — API only called 300ms after typing stops
    const [searchInput, setSearchInput] = useState("");
    const debouncedSearch = useDebounce(searchInput, 300);
    const [filterType, setFilterType] = useState<DrugType | ("")>("");
    const [filterCategory, setFilterCategory] = useState("");
    const [filterActive, setFilterActive] = useState<"" | "true" | "false">("");
    const [showFilters, setShowFilters] = useState(false);

    // Pagination
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [total, setTotal] = useState(0);
    const PAGE_SIZE = 20;

    // Modals
    const [showAddDrug, setShowAddDrug] = useState(false);
    const [editingDrug, setEditingDrug] = useState<Drug | null>(null);
    const [addingBatchFor, setAddingBatchFor] = useState<Drug | null>(null);
    // Category create/edit modal
    const [categoryModal, setCategoryModal] = useState<{
        open: boolean;
        editingCategory?: import("@/types").DrugCategory;
    }>({ open: false });

    // Per-row loading state for toggle so the user gets instant feedback
    const [togglingId, setTogglingId] = useState<string | null>(null);

    // AbortController ref — cancels the previous in-flight request on each fetch
    const abortRef = useRef<AbortController | null>(null);

    // ── Fetch ─────────────────────────────────────────────────
    const fetchDrugs = useCallback(async () => {
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;

        setIsLoading(true);
        setError(null);

        try {
            const result = await drugApi.list(
                {
                    page,
                    page_size: PAGE_SIZE,
                    search: debouncedSearch || undefined,
                    drug_type: filterType || undefined,
                    category_id: filterCategory || undefined,
                    is_active: filterActive === "" ? undefined : filterActive === "true",
                },
                controller.signal
            );

            if (!controller.signal.aborted) {
                setDrugs(result.items);
                setTotalPages(result.total_pages);
                setTotal(result.total);
            }
        } catch (err: unknown) {
            if (err instanceof Error && err.name === "AbortError") return;
            if (!controller.signal.aborted) setError(parseApiError(err));
        } finally {
            if (!controller.signal.aborted) setIsLoading(false);
        }
    }, [page, debouncedSearch, filterType, filterCategory, filterActive]);

    useEffect(() => {
        fetchDrugs();
        return () => abortRef.current?.abort();
    }, [fetchDrugs]);

    // Auto-refresh when any other page mutates drug data (e.g. POS deactivating a drug)
    useAppEvent("drugs:changed", fetchDrugs);

    // Reset to page 1 when filters change (but not when page itself changes)
    const prevFilters = useRef({ debouncedSearch, filterType, filterCategory, filterActive });
    useEffect(() => {
        const prev = prevFilters.current;
        if (
            prev.debouncedSearch !== debouncedSearch ||
            prev.filterType !== filterType ||
            prev.filterCategory !== filterCategory ||
            prev.filterActive !== filterActive
        ) {
            setPage(1);
            prevFilters.current = { debouncedSearch, filterType, filterCategory, filterActive };
        }
    }, [debouncedSearch, filterType, filterCategory, filterActive]);

    // ── Handlers ──────────────────────────────────────────────

    const handleDrugSaved = (saved: Drug) => {
        setShowAddDrug(false);
        setEditingDrug(null);
        if (editingDrug) {
            // Update in-place — no round-trip needed
            setDrugs((prev) => prev.map((d) => (d.id === saved.id ? saved : d)));
        } else {
            // New drug: go to page 1 so it appears
            setPage(1);
            fetchDrugs();
        }
        appEvents.emit("drugs:changed"); // notify other pages
    };

    const handleBatchAdded = (_batch: DrugBatch) => {
        setAddingBatchFor(null);
        fetchDrugs();
        appEvents.emit("inventory:changed"); // notify InventoryPage to refresh
    };

    const handleToggleActive = async (drug: Drug) => {
        setTogglingId(drug.id);
        setError(null);
        // Optimistic update — flip instantly
        setDrugs((prev) => prev.map((d) => (d.id === drug.id ? { ...d, is_active: !d.is_active } : d)));
        try {
            const updated = await drugApi.update(drug.id, { is_active: !drug.is_active });
            setDrugs((prev) => prev.map((d) => (d.id === updated.id ? updated : d)));
            appEvents.emit("drugs:changed");
        } catch (err) {
            // Rollback
            setDrugs((prev) => prev.map((d) => (d.id === drug.id ? drug : d)));
            setError(parseApiError(err));
        } finally {
            setTogglingId(null);
        }
    };

    const activeFiltersCount = [filterType, filterCategory, filterActive].filter(Boolean).length;

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-5 border-b border-slate-200 bg-white">
                <div className="flex items-center justify-between gap-4">
                    <div>
                        <h1 className="font-display text-2xl font-bold text-ink">Drug Catalogue</h1>
                        <p className="text-sm text-ink-muted mt-0.5">
                            {total.toLocaleString()} {total === 1 ? "drug" : "drugs"} in your formulary
                        </p>
                    </div>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={fetchDrugs}
                            className="w-9 h-9 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                            title="Refresh"
                        >
                            <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} />
                        </button>
                        {canEdit && (
                            <>
                                {/* Manage Categories — opens the category modal for create/edit */}
                                <button
                                    onClick={() => setCategoryModal({ open: true })}
                                    className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-ink-secondary border border-slate-200 hover:text-ink hover:bg-slate-50 rounded-xl transition-colors"
                                    title="Manage drug categories"
                                >
                                    <FolderTree className="w-4 h-4" />
                                    Categories
                                </button>
                                <button
                                    onClick={() => setShowAddDrug(true)}
                                    className="flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors"
                                >
                                    <Plus className="w-4 h-4" />
                                    Add Drug
                                </button>
                            </>
                        )}
                    </div>
                </div>

                <div className="flex gap-2 mt-4">
                    <div className="relative flex-1 max-w-md">
                        <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                        <input
                            value={searchInput}
                            onChange={(e) => setSearchInput(e.target.value)}
                            placeholder="Search name, SKU, barcode, manufacturer…"
                            className="w-full pl-9 pr-9 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500"
                        />
                        {searchInput && (
                            <button onClick={() => setSearchInput("")} className="absolute right-2.5 top-2.5 text-ink-muted hover:text-ink">
                                <X className="w-4 h-4" />
                            </button>
                        )}
                    </div>
                    <button
                        onClick={() => setShowFilters(!showFilters)}
                        className={`flex items-center gap-2 px-3 py-2 text-sm rounded-xl border transition-colors ${showFilters || activeFiltersCount > 0
                            ? "border-brand-500 bg-brand-50 text-brand-700"
                            : "border-slate-200 bg-white text-ink-secondary hover:text-ink"
                            }`}
                    >
                        <Filter className="w-4 h-4" />
                        Filters
                        {activeFiltersCount > 0 && (
                            <span className="w-5 h-5 rounded-full bg-brand-600 text-white text-xs flex items-center justify-center">
                                {activeFiltersCount}
                            </span>
                        )}
                    </button>
                </div>

                <AnimatePresence>
                    {showFilters && (
                        <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: "auto" }}
                            exit={{ opacity: 0, height: 0 }}
                            className="overflow-hidden"
                        >
                            <div className="flex gap-3 mt-3 flex-wrap">
                                <select value={filterType} onChange={(e) => setFilterType(e.target.value as DrugType | "")}
                                    className="text-sm rounded-xl border border-slate-200 bg-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500/30">
                                    <option value="">All types</option>
                                    <option value="otc">OTC</option>
                                    <option value="prescription">Prescription</option>
                                    <option value="controlled">Controlled</option>
                                    <option value="herbal">Herbal</option>
                                    <option value="supplement">Supplement</option>
                                </select>

                                <select value={filterCategory} onChange={(e) => setFilterCategory(e.target.value)}
                                    className="text-sm rounded-xl border border-slate-200 bg-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500/30">
                                    <option value="">All categories</option>
                                    {/* FIX: render tree with indentation instead of flat list */}
                                    {renderCategoryOptions(categoryTree)}
                                </select>

                                <select value={filterActive} onChange={(e) => setFilterActive(e.target.value as "" | "true" | "false")}
                                    className="text-sm rounded-xl border border-slate-200 bg-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500/30">
                                    <option value="">Active & Inactive</option>
                                    <option value="true">Active only</option>
                                    <option value="false">Inactive only</option>
                                </select>

                                {activeFiltersCount > 0 && (
                                    <button onClick={() => { setFilterType(""); setFilterCategory(""); setFilterActive(""); }}
                                        className="text-sm text-ink-muted hover:text-ink flex items-center gap-1">
                                        <X className="w-3.5 h-3.5" /> Clear
                                    </button>
                                )}
                            </div>
                        </motion.div>
                    )}
                </AnimatePresence>
            </div>

            {error && (
                <div className="mx-6 mt-4 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-center">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                    <p className="text-sm text-red-600">{error}</p>
                    <button onClick={() => setError(null)} className="ml-auto text-ink-muted hover:text-ink">
                        <X className="w-4 h-4" />
                    </button>
                </div>
            )}

            <div className="flex-1 overflow-auto">
                {isLoading ? (
                    <div className="flex items-center justify-center h-64">
                        <RefreshCw className="w-6 h-6 text-brand-500 animate-spin" />
                    </div>
                ) : drugs.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-64 gap-3 text-ink-muted">
                        <Pill className="w-10 h-10 opacity-30" />
                        <p className="text-sm">No drugs found.{canEdit && " Add your first drug to get started."}</p>
                        {canEdit && (
                            <button onClick={() => setShowAddDrug(true)}
                                className="flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors">
                                <Plus className="w-4 h-4" /> Add Drug
                            </button>
                        )}
                    </div>
                ) : (
                    <table className="w-full text-sm">
                        <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                            <tr>
                                <th className="text-left px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Drug</th>
                                <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Type</th>
                                <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Category</th>
                                <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Price</th>
                                <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Status</th>
                                <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Rx</th>
                                <th className="text-right px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Actions</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 bg-white">
                            {drugs.map((drug) => {
                                const isToggling = togglingId === drug.id;
                                return (
                                    <motion.tr key={drug.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                                        className="hover:bg-slate-50/70 transition-colors group">
                                        <td className="px-6 py-3">
                                            <div className="font-medium text-ink">{drug.name}</div>
                                            <div className="text-xs text-ink-muted mt-0.5 flex items-center gap-2">
                                                {drug.generic_name && <span>{drug.generic_name}</span>}
                                                {drug.sku && <span className="font-mono bg-slate-100 px-1 rounded">{drug.sku}</span>}
                                                {drug.strength && <span>{drug.strength}</span>}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3">
                                            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${DRUG_TYPE_COLORS[drug.drug_type] ?? ""}`}>
                                                {DRUG_TYPE_LABELS[drug.drug_type] ?? drug.drug_type}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3 text-ink-secondary text-xs">
                                            {/* FIX: O(1) map lookup replacing O(n) .find() */}
                                            {categoryMap.get(drug.category_id ?? "") ?? <span className="text-ink-muted">—</span>}
                                        </td>
                                        <td className="px-4 py-3 text-right">
                                            <span className="font-semibold text-ink">₵{Number(drug.unit_price).toFixed(2)}</span>
                                            {drug.cost_price != null && (
                                                <div className="text-xs text-ink-muted">cost: ₵{Number(drug.cost_price).toFixed(2)}</div>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 text-center">
                                            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${drug.is_active ? "bg-green-50 text-green-700" : "bg-slate-100 text-ink-muted"
                                                }`}>
                                                {drug.is_active ? "Active" : "Inactive"}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3 text-center">
                                            {drug.requires_prescription
                                                ? <span className="text-xs font-semibold text-purple-600">Yes</span>
                                                : <span className="text-xs text-ink-muted">No</span>}
                                        </td>
                                        <td className="px-6 py-3">
                                            <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                                <button onClick={() => setAddingBatchFor(drug)}
                                                    className="flex items-center gap-1 px-2 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-50 rounded-lg transition-colors">
                                                    <Package className="w-3.5 h-3.5" /> Stock
                                                </button>
                                                {canEdit && (
                                                    <>
                                                        <button onClick={() => setEditingDrug(drug)}
                                                            className="p-1.5 text-ink-muted hover:text-brand-600 hover:bg-brand-50 rounded-lg transition-colors"
                                                            title="Edit drug">
                                                            <Edit2 className="w-3.5 h-3.5" />
                                                        </button>
                                                        <button
                                                            onClick={() => handleToggleActive(drug)}
                                                            disabled={isToggling}
                                                            className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${drug.is_active
                                                                ? "text-ink-muted hover:text-amber-600 hover:bg-amber-50"
                                                                : "text-ink-muted hover:text-green-600 hover:bg-green-50"
                                                                }`}
                                                            title={drug.is_active ? "Deactivate" : "Activate"}>
                                                            {isToggling
                                                                ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                                                                : drug.is_active
                                                                    ? <ToggleRight className="w-3.5 h-3.5" />
                                                                    : <ToggleLeft className="w-3.5 h-3.5" />}
                                                        </button>
                                                    </>
                                                )}
                                            </div>
                                        </td>
                                    </motion.tr>
                                );
                            })}
                        </tbody>
                    </table>
                )}
            </div>

            {totalPages > 1 && (
                <div className="px-6 py-3 border-t border-slate-200 bg-white flex items-center justify-between">
                    <p className="text-xs text-ink-muted">Page {page} of {totalPages} · {total.toLocaleString()} drugs</p>
                    <div className="flex items-center gap-1">
                        <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}
                            className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40 transition-colors">
                            <ChevronLeft className="w-4 h-4" />
                        </button>
                        {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                            const p = page <= 3 ? i + 1 : page - 2 + i;
                            if (p < 1 || p > totalPages) return null;
                            return (
                                <button key={p} onClick={() => setPage(p)}
                                    className={`w-8 h-8 flex items-center justify-center rounded-lg text-xs font-medium transition-colors ${p === page ? "bg-brand-600 text-white" : "border border-slate-200 text-ink-muted hover:text-ink"
                                        }`}>{p}</button>
                            );
                        })}
                        <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                            className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40 transition-colors">
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    </div>
                </div>
            )}

            <AnimatePresence>
                {(showAddDrug || editingDrug) && (
                    <DrugForm
                        drug={editingDrug ?? undefined}
                        onSuccess={handleDrugSaved}
                        onCancel={() => { setShowAddDrug(false); setEditingDrug(null); }}
                    />
                )}
                {addingBatchFor && activeBranchId && (
                    <AddBatchForm
                        drug={addingBatchFor}
                        branchId={activeBranchId}
                        onSuccess={handleBatchAdded}
                        onCancel={() => setAddingBatchFor(null)}
                    />
                )}
                {categoryModal.open && (
                    <DrugCategoryModal
                        category={categoryModal.editingCategory}
                        categoryTree={categoryTree}
                        onSuccess={() => {
                            // Invalidate tree cache so filter dropdown and DrugForm both refresh
                            invalidateCategories();
                            setCategoryModal({ open: false });
                        }}
                        onCancel={() => setCategoryModal({ open: false })}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}