/**
 * ===================
 * Left panel of the POS. Lets the cashier search the drug catalogue
 * and add items to the cart. Uses the offline-capable local SQLite
 * data when available, falls back to the API on cache miss.
 *
 * Features:
 * - Debounced search (300ms)
 * - Drug type filter badges
 * - Keyboard navigation (↑↓ arrows, Enter to add)
 * - Prescription badge warning
 * - Stock status indicator
 * - Out-of-stock prevention
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { Search, Plus, X, ShieldAlert, Package } from "lucide-react";
import { inventoryApi } from "@/api/inventory";
import { localRead } from "@/lib/localRead";
import { cacheBranchInventoryRows } from "@/lib/localDb";
import { isBackendReachable, isOfflineError } from "@/api/client";
import { useDebounce } from "@/hooks/useDebounce";
import { parseApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import type { BranchInventoryWithDetails, Drug, DrugType } from "@/types";

interface DrugSearchPanelProps {
    onAdd: (drug: Drug) => void;
    disabledDrugIds?: Set<string>;
}

const TYPE_FILTER: Array<{ value: DrugType | ""; label: string }> = [
    { value: "", label: "All" },
    { value: "otc", label: "OTC" },
    { value: "prescription", label: "Rx" },
    { value: "controlled", label: "Controlled" },
    { value: "herbal", label: "Herbal" },
    { value: "supplement", label: "Supplement" },
];

const TYPE_COLORS: Record<string, string> = {
    otc: "bg-blue-50 text-blue-700",
    prescription: "bg-purple-50 text-purple-700",
    controlled: "bg-red-50 text-red-700",
    herbal: "bg-green-50 text-green-700",
    supplement: "bg-amber-50 text-amber-700",
};

function toBoolean(value: unknown): boolean {
    return value === true || value === 1 || value === "1" || value === "true";
}

function inventoryItemToDrug(item: BranchInventoryWithDetails): Drug {
    const now = item.updated_at || new Date().toISOString();
    const row = item as unknown as Record<string, unknown>;
    const requiresPrescription = toBoolean(row.requires_prescription);
    const drugType = (row.drug_type as DrugType | undefined)
        ?? (requiresPrescription ? "prescription" : "otc");
    const unitPrice = Number(
        item.effective_unit_price ??
        item.selling_price ??
        item.drug_unit_price ??
        row.unit_price ??
        0
    );
    const taxRate = Number(row.tax_rate ?? 0);
    const reorderLevel = Number(item.drug_reorder_level ?? 0);
    const reorderQuantity = Number(row.reorder_quantity ?? 0);
    return {
        id: item.drug_id,
        organization_id: String(row.organization_id ?? ""),
        name: item.drug_name || item.drug_id,
        generic_name: (row.drug_generic_name as string | null | undefined) ?? null,
        brand_name: null,
        sku: item.drug_sku,
        barcode: null,
        category_id: null,
        drug_type: drugType,
        dosage_form: null,
        strength: (row.drug_strength as string | null | undefined) ?? null,
        manufacturer: null,
        supplier: null,
        ndc_code: null,
        requires_prescription: requiresPrescription,
        controlled_substance_schedule: null,
        unit_price: Number.isFinite(unitPrice) ? unitPrice : 0,
        cost_price: null,
        markup_percentage: null,
        tax_rate: Number.isFinite(taxRate) ? taxRate : 0,
        reorder_level: Number.isFinite(reorderLevel) ? reorderLevel : 0,
        reorder_quantity: Number.isFinite(reorderQuantity) ? reorderQuantity : 0,
        max_stock_level: null,
        unit_of_measure: "unit",
        description: null,
        usage_instructions: null,
        side_effects: null,
        contraindications: null,
        storage_conditions: null,
        image_url: null,
        is_active: true,
        profit_margin: null,
        is_deleted: false,
        deleted_at: null,
        deleted_by: null,
        sync_status: item.sync_status,
        sync_version: item.sync_version,
        synced_at: item.synced_at,
        created_at: item.created_at || now,
        updated_at: now,
    };
}

export function DrugSearchPanel({ onAdd, disabledDrugIds }: DrugSearchPanelProps) {
    const [query, setQuery] = useState("");
    const [typeFilter, setTypeFilter] = useState<DrugType | "">("");
    const [drugs, setDrugs] = useState<Drug[]>([]);
    const [stockQuantities, setStockQuantities] = useState<Record<string, number>>({});
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [focusedIndex, setFocusedIndex] = useState(-1);

    const [page, setPage] = useState(1);
    const [hasMore, setHasMore] = useState(false);
    const [isLoadingMore, setIsLoadingMore] = useState(false);

    const debouncedQuery = useDebounce(query, 300);
    const inputRef = useRef<HTMLInputElement>(null);
    const abortRef = useRef<AbortController | null>(null);
    const activeBranchId = useAuthStore((state) => state.activeBranchId);

    const fetchDrugs = useCallback(async (pageNum: number, append: boolean) => {
        if (!activeBranchId) {
            setDrugs([]);
            setHasMore(false);
            return;
        }

        if (append) {
            setIsLoadingMore(true);
        } else {
            setIsLoading(true);
            setError(null);
            setFocusedIndex(-1);
            setPage(1);
            abortRef.current?.abort();
            const controller = new AbortController();
            abortRef.current = controller;
        }

        try {
            const pageSize = 30;
            const signal = append ? undefined : abortRef.current?.signal;

            if (!navigator.onLine || !isBackendReachable()) {
                const result = await localRead.getBranchInventory(
                    activeBranchId,
                    {
                        search: debouncedQuery || undefined,
                        include_zero_stock: false,
                        drug_type: typeFilter || undefined,
                    },
                    pageNum,
                    pageSize
                );
                
                const newDrugs = result.items.map(inventoryItemToDrug);
                setDrugs((prev) => {
                    const next = append ? [...prev, ...newDrugs] : newDrugs;
                    setHasMore(next.length < result.total && newDrugs.length === pageSize);
                    return next;
                });

                const sq: Record<string, number> = {};
                for (const item of result.items) {
                    const vbq = item.valid_batch_quantity ?? item.available_quantity ?? item.quantity;
                    sq[item.drug_id] = vbq;
                }
                setStockQuantities((prev) => ({ ...prev, ...sq }));
                return;
            }

            const result = await inventoryApi.getBranchInventory(
                activeBranchId,
                {
                    page: pageNum,
                    page_size: pageSize,
                    search: debouncedQuery || undefined,
                    include_zero_stock: false,
                    drug_type: typeFilter || undefined,
                },
                signal
            );

            const isAborted = signal?.aborted ?? false;
            if (!isAborted) {
                const newDrugs = result.items.map(inventoryItemToDrug);
                setDrugs((prev) => {
                    const next = append ? [...prev, ...newDrugs] : newDrugs;
                    setHasMore(next.length < result.total && newDrugs.length === pageSize);
                    return next;
                });

                const sq: Record<string, number> = {};
                for (const item of result.items) {
                    const vbq = item.valid_batch_quantity ?? item.available_quantity ?? item.quantity;
                    sq[item.drug_id] = vbq;
                }
                setStockQuantities((prev) => ({ ...prev, ...sq }));
                
                if (!append) {
                    cacheBranchInventoryRows(result.items).catch((e: unknown) =>
                        console.error("[DrugSearchPanel] cacheBranchInventoryRows:", typeof e === "object" && e !== null ? JSON.stringify(e) : String(e))
                    );
                }
            }
        } catch (err: unknown) {
            if (err instanceof Error && err.name === "AbortError") return;
            if (!append && !abortRef.current?.signal.aborted && isOfflineError(err)) {
                try {
                    const result = await localRead.getBranchInventory(
                        activeBranchId,
                        {
                            search: debouncedQuery || undefined,
                            include_zero_stock: false,
                            drug_type: typeFilter || undefined,
                        },
                        pageNum,
                        30
                    );
                    if (!abortRef.current?.signal.aborted) {
                        const newDrugs = result.items.map(inventoryItemToDrug);
                        setDrugs(newDrugs);
                        const sq: Record<string, number> = {};
                        for (const item of result.items) {
                            const vbq = item.valid_batch_quantity ?? item.available_quantity ?? item.quantity;
                            sq[item.drug_id] = vbq;
                        }
                        setStockQuantities(sq);
                        setHasMore(newDrugs.length < result.total && newDrugs.length === 30);
                    }
                } catch (localErr) {
                    if (!abortRef.current?.signal.aborted) setError(parseApiError(localErr));
                }
                return;
            }
            setError(parseApiError(err));
        } finally {
            if (!append) {
                setIsLoading(false);
            } else {
                setIsLoadingMore(false);
            }
        }
    }, [debouncedQuery, typeFilter, activeBranchId]);

    const handleLoadMore = useCallback(() => {
        if (isLoadingMore || !hasMore) return;
        const nextPage = page + 1;
        setPage(nextPage);
        fetchDrugs(nextPage, true);
    }, [page, isLoadingMore, hasMore, fetchDrugs]);

    useEffect(() => {
        fetchDrugs(1, false);
        return () => abortRef.current?.abort();
    }, [fetchDrugs]);

    // Keyboard nav
    const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (drugs.length === 0) return;
        if (e.key === "ArrowDown") {
            e.preventDefault();
            setFocusedIndex((i) => Math.min(i + 1, drugs.length - 1));
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setFocusedIndex((i) => Math.max(i - 1, -1));
        } else if (e.key === "Enter" && focusedIndex >= 0) {
            e.preventDefault();
            const drug = drugs[focusedIndex];
            if (drug && !isOutOfStock(drug) && !disabledDrugIds?.has(drug.id)) onAdd(drug);
        }
    };

    const getStockQuantity = useCallback((drug: Drug): number => {
        return stockQuantities[drug.id] ?? 0;
    }, [stockQuantities]);
    const isOutOfStock = useCallback((drug: Drug): boolean => {
        return (stockQuantities[drug.id] ?? 0) <= 0;
    }, [stockQuantities]);

    return (
        <div className="flex flex-col h-full">
            {/* Search input */}
            <div className="p-4 border-b border-slate-100">
                <div className="relative">
                    <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                    <input
                        ref={inputRef}
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder="Search drug, SKU, barcode…"
                        autoFocus
                        className="w-full pl-9 pr-9 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500"
                    />
                    {query && (
                        <button
                            onClick={() => { setQuery(""); inputRef.current?.focus(); }}
                            className="absolute right-2.5 top-2.5 text-ink-muted hover:text-ink"
                        >
                            <X className="w-4 h-4" />
                        </button>
                    )}
                </div>

                {/* Type filter pills */}
                <div className="flex gap-1.5 mt-2.5 flex-wrap">
                    {TYPE_FILTER.map((f) => (
                        <button
                            key={f.value}
                            onClick={() => setTypeFilter(f.value)}
                            className={`px-2.5 py-1 text-xs font-medium rounded-full transition-colors ${typeFilter === f.value
                                    ? "bg-brand-600 text-white"
                                    : "bg-slate-100 text-ink-secondary hover:bg-slate-200"
                                }`}
                        >
                            {f.label}
                        </button>
                    ))}
                </div>
            </div>

            {/* Drug list */}
            <div className="flex-1 overflow-y-auto">
                {isLoading ? (
                    <div className="flex items-center justify-center h-32">
                        <div className="w-5 h-5 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
                    </div>
                ) : error ? (
                    <div className="p-4 text-sm text-red-600 bg-red-50 m-3 rounded-xl">{error}</div>
                ) : drugs.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-32 gap-2 text-ink-muted">
                        <Package className="w-8 h-8 opacity-30" />
                        <p className="text-xs">
                            {query ? "No drugs match your search" : "No drugs available"}
                        </p>
                    </div>
                ) : (
                    <div className="p-2 space-y-1">
                        {drugs.map((drug, idx) => {
                            const isDisabled = disabledDrugIds?.has(drug.id);
                            const isFocused = idx === focusedIndex;
                            const unitPrice = Number(drug.unit_price);
                            const stockQty = getStockQuantity(drug);
                            const hasValidStock = stockQty > 0;
                            const canAdd = !isDisabled && hasValidStock;
                            return (
                                <button
                                    key={drug.id}
                                    onClick={() => canAdd && onAdd(drug)}
                                    disabled={!canAdd}
                                    className={`w-full flex items-center justify-between p-3 rounded-xl text-left transition-all ${isFocused
                                            ? "bg-brand-50 ring-1 ring-brand-300"
                                            : !canAdd
                                                ? "bg-slate-50 opacity-60 cursor-default"
                                                : "hover:bg-slate-50 active:bg-slate-100"
                                        }`}
                                >
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2">
                                            <span className="text-sm font-semibold text-ink truncate">
                                                {drug.name}
                                            </span>
                                            <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full flex-shrink-0 ${TYPE_COLORS[drug.drug_type] ?? "bg-slate-100 text-ink-muted"}`}>
                                                {drug.drug_type.toUpperCase()}
                                            </span>
                                            {drug.requires_prescription && (
                                                <span title="Requires prescription" className="flex-shrink-0">
                                                    <ShieldAlert className="w-3.5 h-3.5 text-purple-500" />
                                                </span>
                                            )}
                                        </div>
                                        <div className="text-xs text-ink-muted mt-0.5 flex items-center gap-2">
                                            {drug.generic_name && <span className="truncate">{drug.generic_name}</span>}
                                            {drug.strength && <span>{drug.strength}</span>}
                                            {drug.sku && (
                                                <span className="font-mono bg-slate-100 px-1 rounded text-[10px]">
                                                    {drug.sku}
                                                </span>
                                            )}
                                        </div>
                                        <div className="text-xs mt-0.5">
                                            {!hasValidStock ? (
                                                <span className="text-red-500 font-medium">Not stocked at this branch</span>
                                            ) : (
                                                <span className="text-green-600 font-medium">{stockQty} available</span>
                                            )}
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                                        <span className="text-sm font-bold text-ink">
                                            ₵{(Number.isFinite(unitPrice) ? unitPrice : 0).toFixed(2)}
                                        </span>
                                        {isDisabled ? (
                                            <span className="text-xs text-brand-600 font-semibold">In cart</span>
                                        ) : !hasValidStock ? (
                                            <span className="text-xs text-red-500 font-semibold">Unavailable</span>
                                        ) : (
                                            <div className="w-7 h-7 rounded-lg bg-brand-600 flex items-center justify-center flex-shrink-0">
                                                <Plus className="w-4 h-4 text-white" />
                                            </div>
                                        )}
                                    </div>
                                </button>
                            );
                        })}

                        {hasMore && (
                            <div className="p-4 flex justify-center">
                                <button
                                    disabled={isLoadingMore}
                                    onClick={handleLoadMore}
                                    className="px-4 py-2 text-xs font-semibold text-brand-600 hover:text-brand-700 bg-brand-50 hover:bg-brand-100 rounded-full transition-colors disabled:opacity-60 disabled:cursor-wait"
                                >
                                    {isLoadingMore ? "Loading more..." : "Load more items"}
                                </button>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
