import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { AnimatePresence } from "framer-motion";
import {
    Search, Package, AlertTriangle, Clock, RefreshCw,
    ChevronLeft, ChevronRight, X, TrendingDown, Plus, BarChart3,
    Layers, ArrowRightLeft, SlidersHorizontal,
    Wrench, Timer, ShieldOff, Undo2, ClipboardEdit,
    Trash2, Building2,
} from "lucide-react";
import { inventoryApi } from "@/api/inventory";
import { drugApi } from "@/api/drugs";
import { branchApi } from "@/api/branches";
import { localRead } from "@/lib/localRead";
import { cacheBranchInventoryRows } from "@/lib/localDb";
import { isBackendReachable, isOfflineError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { useDebounce } from "@/hooks/useDebounce";
import { AddBatchForm } from "@/components/inventory/AddBatchForm";
import { EditBatchForm } from "@/components/inventory/EditBatchForm";
import { parseApiError } from "@/api/client";
import { appEvents, useAppEvent } from "@/lib/events";
import { withTimeout } from "@/lib/withTimeout";
import { DataFreshnessIndicator } from "@/components/DataFreshnessIndicator";
import type {
    BranchInventoryWithDetails,
    LowStockItem,
    ExpiringBatchItem,
    InventoryValuationResponse,
    Drug,
    DrugBatch,
    StockAdjustmentCreate,
    BranchListItem,
} from "@/types";

type ActiveView = "stock" | "low_stock" | "expiring" | "valuation";
type SidePanel = "batches" | "adjust" | "transfer" | "settings" | null;

// ─── Shared styles ────────────────────────────────────────────────────────────

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 " +
    "transition-colors placeholder:text-slate-400 disabled:bg-slate-50 disabled:text-slate-400";

const labelCls =
    "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

// ─── BatchViewerPanel ─────────────────────────────────────────────────────────
//
// Displays the FEFO-ordered batch breakdown for a specific drug in this branch.
// Backend: GET /inventory/batches/drug/{drug_id}?branch_id={id}
//
// Previously there was no way to see which batches were in stock or when they
// expire — users had to guess or consult the expiring-soon report separately.

function BatchViewerPanel({
    item,
    branchId,
    onClose,
}: {
    item: BranchInventoryWithDetails;
    branchId: string;
    onClose: () => void;
}) {
    const [batches, setBatches] = useState<DrugBatch[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [editingBatch, setEditingBatch] = useState<DrugBatch | null>(null);

    const load = useCallback(async () => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        try {
            const r = !navigator.onLine || !isBackendReachable()
                ? await localRead.getBatchesForDrug(item.drug_id, {
                    branch_id: branchId,
                    include_expired: true,
                    include_empty: true,
                })
                : await inventoryApi.getBatches(item.drug_id, {
                    branch_id: branchId,
                    include_expired: true,
                    include_empty: true,
                });
            if (!cancelled) setBatches(r.items);
        } catch (err) {
            if (!cancelled && isOfflineError(err)) {
                const r = await localRead.getBatchesForDrug(item.drug_id, {
                    branch_id: branchId,
                    include_expired: true,
                    include_empty: true,
                });
                if (!cancelled) setBatches(r.items);
                return;
            }
            if (!cancelled) setError(parseApiError(err));
        } finally {
            if (!cancelled) setLoading(false);
        }
    }, [item.drug_id, branchId]);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        const load = async () => {
            try {
                const r = !navigator.onLine || !isBackendReachable()
                    ? await localRead.getBatchesForDrug(item.drug_id, {
                        branch_id: branchId,
                        include_expired: false,
                        include_empty: false,
                    })
                    : await inventoryApi.getBatches(item.drug_id, {
                        branch_id: branchId,
                        include_expired: false,
                        include_empty: false,
                    });
                if (!cancelled) setBatches(r.items);
            } catch (err) {
                if (!cancelled && isOfflineError(err)) {
                    const r = await localRead.getBatchesForDrug(item.drug_id, {
                        branch_id: branchId,
                        include_expired: false,
                        include_empty: false,
                    });
                    if (!cancelled) setBatches(r.items);
                    return;
                }
                if (!cancelled) setError(parseApiError(err));
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        load();
    }, [load]);

    const available = item.available_quantity ?? (item.quantity - item.reserved_quantity);

    const handleEditSuccess = () => {
        setEditingBatch(null);
        load();
        appEvents.emit("inventory:changed");
    };

    return (
        <div className="flex flex-col h-full bg-white">
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div className="min-w-0">
                    <h2 className="text-base font-bold text-ink">Batch Breakdown</h2>
                    <p className="text-xs text-ink-muted mt-0.5 truncate">{item.drug_name}</p>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 flex-shrink-0 ml-2">
                    <X className="w-4 h-4" />
                </button>
            </div>

            {/* Stock summary strip */}
            <div className="flex items-center gap-6 px-5 py-3 bg-slate-50 border-b border-slate-100 flex-shrink-0">
                {[
                    { label: "Available", value: available.toLocaleString(), color: available === 0 ? "text-red-600" : "text-ink" },
                    { label: "Total", value: item.quantity.toLocaleString(), color: "text-ink" },
                ].map(({ label, value, color }) => (
                    <div key={label} className="text-center">
                        <p className={`text-lg font-bold ${color}`}>{value}</p>
                        <p className="text-[10px] text-slate-400 uppercase tracking-wide">{label}</p>
                    </div>
                ))}
            </div>

            <div className="flex-1 overflow-y-auto">
                {loading ? (
                    <div className="flex items-center justify-center h-32">
                        <RefreshCw className="w-5 h-5 text-brand-500 animate-spin" />
                    </div>
                ) : error ? (
                    <div className="mx-4 mt-4 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600 flex items-start gap-2">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />{error}
                    </div>
                ) : batches.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-32 gap-2 text-ink-muted">
                        <Layers className="w-8 h-8 opacity-30" />
                        <p className="text-sm">No active batches found</p>
                    </div>
                ) : (
                    <div className="divide-y divide-slate-100">
                        {batches.map((batch) => {
                            const daysLeft = batch.days_until_expiry
                                ?? Math.floor((new Date(batch.expiry_date).getTime() - Date.now()) / 86_400_000);
                            const urgency = daysLeft <= 30 ? "red" : daysLeft <= 60 ? "amber" : "green";
                            return (
                                <div key={batch.id} className={`px-5 py-3.5 group relative ${urgency === "red" ? "bg-red-50/40" : ""}`}>
                                    <div className="flex items-center justify-between gap-2 mb-2">
                                        <div className="flex items-center gap-2">
                                            <span className="font-mono text-xs bg-slate-100 px-2 py-0.5 rounded font-medium text-ink">
                                                {batch.batch_number}
                                            </span>
                                            <button
                                                onClick={() => setEditingBatch(batch)}
                                                className="p-1 rounded bg-slate-100 text-slate-400 hover:text-brand-600 hover:bg-brand-50 opacity-0 group-hover:opacity-100 transition-opacity"
                                                title="Edit batch"
                                            >
                                                <ClipboardEdit className="w-3 h-3" />
                                            </button>
                                        </div>
                                        <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${urgency === "red" ? "bg-red-100 text-red-700"
                                            : urgency === "amber" ? "bg-amber-100 text-amber-700"
                                                : "bg-green-100 text-green-700"}`}>
                                            {daysLeft}d left
                                        </span>
                                    </div>
                                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-ink-secondary">
                                        <div>
                                            <span className="text-slate-400">Remaining: </span>
                                            <span className="font-semibold text-ink">{batch.remaining_quantity}</span>
                                            <span className="text-slate-400"> / {batch.quantity}</span>
                                        </div>
                                        <div>
                                            <span className="text-slate-400">Expiry: </span>
                                            <span className="font-medium">
                                                {new Date(batch.expiry_date).toLocaleDateString("en-GH", {
                                                    day: "numeric", month: "short", year: "numeric",
                                                })}
                                            </span>
                                        </div>
                                        {batch.cost_price != null && (
                                            <div>
                                                <span className="text-slate-400">Cost: </span>
                                                <span className="font-medium">₵{Number(batch.cost_price).toFixed(2)}</span>
                                            </div>
                                        )}
                                        {batch.selling_price != null && (
                                            <div>
                                                <span className="text-slate-400">Sell: </span>
                                                <span className="font-medium text-brand-600">₵{Number(batch.selling_price).toFixed(2)}</span>
                                            </div>
                                        )}
                                        {batch.supplier && (
                                            <div className="col-span-2 truncate">
                                                <span className="text-slate-400">Supplier: </span>
                                                <span>{batch.supplier}</span>
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>

            <AnimatePresence>
                {editingBatch && (
                    <EditBatchForm
                        batch={editingBatch}
                        drugName={item.drug_name}
                        onSuccess={handleEditSuccess}
                        onCancel={() => setEditingBatch(null)}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}

// ─── AdjustStockPanel ─────────────────────────────────────────────────────────
//
// Records damage, expiry write-offs, theft, customer returns, and count
// corrections. Each creates an auditable StockAdjustment record on the server.
// Backend: POST /inventory/adjust   (requires manage_inventory permission)
//
// Previously this endpoint had no UI — staff could only add stock via batch
// creation, with no mechanism to record losses or corrections.

type AdjustmentFormType = "damage" | "expired" | "theft" | "return" | "correction";

const ADJUSTMENT_OPTIONS: {
    value: AdjustmentFormType;
    label: string;
    description: string;
    icon: React.ElementType;
    direction: "negative" | "positive" | "both";
    activeColor: string;
}[] = [
        { value: "damage", label: "Damage", description: "Damaged goods write-off", icon: Wrench, direction: "negative", activeColor: "border-red-400 bg-red-50" },
        { value: "expired", label: "Expired", description: "Expired stock removal", icon: Timer, direction: "negative", activeColor: "border-orange-400 bg-orange-50" },
        { value: "theft", label: "Theft", description: "Reported stock loss", icon: ShieldOff, direction: "negative", activeColor: "border-red-400 bg-red-50" },
        { value: "return", label: "Return", description: "Customer return accepted", icon: Undo2, direction: "positive", activeColor: "border-green-400 bg-green-50" },
        { value: "correction", label: "Correction", description: "Physical count adjustment", icon: ClipboardEdit, direction: "both", activeColor: "border-blue-400 bg-blue-50" },
    ];

function AdjustStockPanel({
    item,
    branchId,
    onSuccess,
    onClose,
}: {
    item: BranchInventoryWithDetails;
    branchId: string;
    onSuccess: () => void;
    onClose: () => void;
}) {
    const [adjustType, setAdjustType] = useState<AdjustmentFormType>("damage");
    const [quantity, setQuantity] = useState("");
    const [correctionDir, setCorrectionDir] = useState<"+" | "-">("-");
    const [reason, setReason] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const selected = ADJUSTMENT_OPTIONS.find((o) => o.value === adjustType)!;
    const available = item.available_quantity ?? (item.quantity - item.reserved_quantity);

    const qty = parseInt(quantity) || 0;
    const isNegative =
        selected.direction === "negative" ||
        (selected.direction === "both" && correctionDir === "-");
    const quantityChange = isNegative ? -qty : qty;
    const projectedStock = Math.max(0, available + quantityChange);

    const canSubmit = qty > 0 && !submitting;

    const handleSubmit = async () => {
        if (!canSubmit) return;
        setSubmitting(true);
        setError(null);
        try {
            const payload: StockAdjustmentCreate = {
                branch_id: branchId,
                drug_id: item.drug_id,
                adjustment_type: adjustType,
                quantity_change: quantityChange,
                reason: reason.trim() || undefined,
            };
            await inventoryApi.adjust(payload);
            onSuccess();
        } catch (err) {
            setError(parseApiError(err));
            setSubmitting(false);
        }
    };

    return (
        <div className="flex flex-col h-full bg-white">
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div className="min-w-0">
                    <h2 className="text-base font-bold text-ink">Adjust Stock</h2>
                    <p className="text-xs text-ink-muted mt-0.5 truncate">{item.drug_name}</p>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 flex-shrink-0 ml-2">
                    <X className="w-4 h-4" />
                </button>
            </div>

            <div className="px-5 py-2.5 bg-slate-50 border-b border-slate-100 flex-shrink-0">
                <span className="text-xs text-slate-500">Current available: </span>
                <span className={`text-sm font-bold ${available === 0 ? "text-red-600" : available <= item.drug_reorder_level ? "text-amber-600" : "text-ink"}`}>
                    {available.toLocaleString()} units
                </span>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
                {error && (
                    <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />{error}
                    </div>
                )}

                {/* Type selector */}
                <div>
                    <label className={labelCls}>Adjustment Type</label>
                    <div className="space-y-1.5">
                        {ADJUSTMENT_OPTIONS.map((opt) => {
                            const Icon = opt.icon;
                            const active = adjustType === opt.value;
                            return (
                                <button
                                    key={opt.value}
                                    type="button"
                                    onClick={() => setAdjustType(opt.value)}
                                    className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border text-left transition-colors ${active ? opt.activeColor : "border-slate-200 hover:bg-slate-50"}`}
                                >
                                    <Icon className={`w-4 h-4 flex-shrink-0 ${active ? "text-ink" : "text-slate-400"}`} />
                                    <div>
                                        <p className="text-sm font-semibold text-ink">{opt.label}</p>
                                        <p className="text-xs text-slate-400">{opt.description}</p>
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                </div>

                {/* Direction toggle (correction only) */}
                {selected.direction === "both" && (
                    <div>
                        <label className={labelCls}>Direction</label>
                        <div className="flex rounded-xl border border-slate-200 overflow-hidden text-xs font-semibold">
                            {(["+", "-"] as const).map((d) => (
                                <button key={d} type="button" onClick={() => setCorrectionDir(d)}
                                    className={`flex-1 py-2.5 transition-colors ${correctionDir === d ? "bg-brand-600 text-white" : "text-slate-500 hover:bg-slate-50"}`}>
                                    {d === "+" ? "Add (+)" : "Remove (−)"}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                {/* Quantity */}
                <div>
                    <label className={labelCls}>Quantity</label>
                    <input
                        type="number"
                        min={1}
                        value={quantity}
                        onChange={(e) => setQuantity(e.target.value.replace(/[^0-9]/g, ""))}
                        placeholder="Enter quantity"
                        className={inputCls}
                    />
                    {qty > 0 && (
                        <p className={`text-xs mt-1.5 font-medium ${isNegative ? "text-red-600" : "text-green-600"}`}>
                            Stock will {isNegative ? "decrease" : "increase"} by {qty} →{" "}
                            <span className="font-bold">{projectedStock} units</span>
                        </p>
                    )}
                </div>

                {/* Reason */}
                <div>
                    <label className={labelCls}>
                        Reason{" "}
                        <span className="text-slate-400 normal-case tracking-normal font-normal">(optional)</span>
                    </label>
                    <textarea
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        placeholder="Add a note for the audit trail…"
                        rows={3}
                        maxLength={500}
                        className="w-full px-3 py-2.5 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors resize-none placeholder:text-slate-400"
                    />
                </div>
            </div>

            <div className="flex-shrink-0 border-t border-slate-200 px-4 py-4 flex items-center justify-end gap-3">
                <button onClick={onClose} disabled={submitting} type="button"
                    className="px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors">
                    Cancel
                </button>
                <button onClick={handleSubmit} disabled={!canSubmit} type="button"
                    className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2">
                    {submitting
                        ? <><RefreshCw className="w-4 h-4 animate-spin" /> Saving…</>
                        : <><SlidersHorizontal className="w-4 h-4" /> Record Adjustment</>}
                </button>
            </div>
        </div>
    );
}

function BranchDrugSettingsPanel({
    item,
    branchId,
    onSuccess,
    onClose,
}: {
    item: BranchInventoryWithDetails;
    branchId: string;
    onSuccess: () => void;
    onClose: () => void;
}) {
    const [location, setLocation] = useState(item.location ?? "");
    const [sellingPrice, setSellingPrice] = useState(item.selling_price != null ? String(item.selling_price) : "");
    const [submitting, setSubmitting] = useState(false);
    const [removing, setRemoving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const save = async () => {
        setSubmitting(true);
        setError(null);
        try {
            await inventoryApi.updateBranchDrug(branchId, item.drug_id, {
                location: location.trim() || null,
                selling_price: sellingPrice === "" ? null : Number(sellingPrice),
            });
            onSuccess();
        } catch (err) {
            setError(parseApiError(err));
            setSubmitting(false);
        }
    };

    const remove = async () => {
        setRemoving(true);
        setError(null);
        try {
            await inventoryApi.removeBranchDrug(branchId, item.drug_id);
            onSuccess();
        } catch (err) {
            setError(parseApiError(err));
            setRemoving(false);
        }
    };

    return (
        <div className="flex flex-col h-full bg-white">
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div className="min-w-0">
                    <h2 className="text-base font-bold text-ink">Branch Settings</h2>
                    <p className="text-xs text-ink-muted mt-0.5 truncate">{item.drug_name}</p>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 flex-shrink-0 ml-2">
                    <X className="w-4 h-4" />
                </button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
                {error && (
                    <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />{error}
                    </div>
                )}

                <div>
                    <label className={labelCls}>Shelf Location</label>
                    <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="e.g. Aisle 2 / Shelf B" className={inputCls} />
                </div>

                <div>
                    <label className={labelCls}>Branch Selling Price</label>
                    <div className="relative">
                        <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                        <input type="number" min="0" step="0.01" value={sellingPrice} onChange={(e) => setSellingPrice(e.target.value)} placeholder={Number(item.catalog_unit_price ?? item.drug_unit_price).toFixed(2)} className={`${inputCls} pl-7`} />
                    </div>
                    <p className="text-xs text-ink-muted mt-1">Leave empty to use ₵{Number(item.catalog_unit_price ?? item.drug_unit_price).toFixed(2)} from the organization catalogue.</p>
                </div>

                <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
                    <p className="text-xs text-ink-muted">Current branch price</p>
                    <p className="text-lg font-bold text-ink mt-0.5">₵{Number(item.effective_unit_price).toFixed(2)}</p>
                </div>
            </div>

            <div className="px-5 py-4 border-t border-slate-100 flex items-center justify-between gap-2">
                <button type="button" onClick={remove} disabled={removing || submitting || item.quantity > 0 || item.reserved_quantity > 0}
                    title={item.quantity > 0 || item.reserved_quantity > 0 ? "Only zero-stock drugs can be removed" : "Remove from branch"}
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-xl text-sm font-semibold text-red-600 hover:bg-red-50 disabled:opacity-40 disabled:hover:bg-transparent">
                    {removing ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                    Remove
                </button>
                <button type="button" onClick={save} disabled={submitting || removing}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 disabled:opacity-50">
                    {submitting && <RefreshCw className="w-4 h-4 animate-spin" />}
                    Save
                </button>
            </div>
        </div>
    );
}

function AddBranchDrugModal({
    branchId,
    existingDrugIds,
    onAdded,
    onClose,
}: {
    branchId: string;
    existingDrugIds: Set<string>;
    onAdded: () => void;
    onClose: () => void;
}) {
    const [query, setQuery] = useState("");
    const debouncedQuery = useDebounce(query, 250);
    const [drugs, setDrugs] = useState<Drug[]>([]);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [location, setLocation] = useState("");
    const [sellingPrice, setSellingPrice] = useState("");
    const [loading, setLoading] = useState(true);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        const ctrl = new AbortController();
        setLoading(true);
        setError(null);
        drugApi.list({ search: debouncedQuery || undefined, is_active: true, page: 1, page_size: 30 }, ctrl.signal)
            .then((result) => setDrugs(result.items.filter((drug) => !existingDrugIds.has(drug.id))))
            .catch((err) => { if (!ctrl.signal.aborted) setError(parseApiError(err)); })
            .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
        return () => ctrl.abort();
    }, [debouncedQuery, existingDrugIds]);

    const selectedDrug = drugs.find((drug) => drug.id === selectedId) ?? null;

    const add = async () => {
        if (!selectedDrug) return;
        setSubmitting(true);
        setError(null);
        try {
            await inventoryApi.addDrugToBranch(branchId, {
                branch_id: branchId,
                drug_id: selectedDrug.id,
                quantity: 0,
                reserved_quantity: 0,
                location: location.trim() || null,
                selling_price: sellingPrice === "" ? null : Number(sellingPrice),
            });
            onAdded();
        } catch (err) {
            setError(parseApiError(err));
            setSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
            <div className="w-full max-w-2xl bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]">
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
                    <div>
                        <h2 className="font-display text-lg font-bold text-ink">Add Drugs</h2>
                        <p className="text-xs text-ink-muted mt-0.5">Choose from the organization catalogue for this branch.</p>
                    </div>
                    <button type="button" onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="grid grid-cols-[1.2fr_0.8fr] min-h-0 flex-1">
                    <div className="border-r border-slate-100 flex flex-col min-h-0">
                        <div className="p-4 border-b border-slate-100">
                            <div className="relative">
                                <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search facility-wide drugs..." className="w-full pl-9 pr-3 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500" />
                            </div>
                        </div>
                        <div className="flex-1 overflow-y-auto p-2">
                            {loading ? (
                                <div className="h-28 flex items-center justify-center"><RefreshCw className="w-5 h-5 animate-spin text-brand-500" /></div>
                            ) : error ? (
                                <div className="m-2 p-3 rounded-xl bg-red-50 text-sm text-red-600">{error}</div>
                            ) : drugs.length === 0 ? (
                                <div className="h-28 flex items-center justify-center text-sm text-ink-muted">No catalogue drugs available</div>
                            ) : (
                                drugs.map((drug) => (
                                    <button key={drug.id} type="button" onClick={() => { setSelectedId(drug.id); if (sellingPrice === "") setSellingPrice(String(drug.unit_price)); }}
                                        className={`w-full text-left px-3 py-2.5 rounded-xl transition-colors ${selectedId === drug.id ? "bg-brand-50 ring-1 ring-brand-200" : "hover:bg-slate-50"}`}>
                                        <div className="flex items-center justify-between gap-3">
                                            <div className="min-w-0">
                                                <p className="text-sm font-semibold text-ink truncate">{drug.name}</p>
                                                <p className="text-xs text-ink-muted truncate">{[drug.sku, drug.generic_name, drug.strength].filter(Boolean).join(" · ")}</p>
                                            </div>
                                            <span className="text-sm font-bold text-ink">₵{drug.unit_price.toFixed(2)}</span>
                                        </div>
                                    </button>
                                ))
                            )}
                        </div>
                    </div>

                    <div className="p-5 space-y-4">
                        <div>
                            <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide">Selected Drug</p>
                            <p className="text-sm font-bold text-ink mt-1">{selectedDrug?.name ?? "None selected"}</p>
                        </div>
                        <div>
                            <label className={labelCls}>Shelf Location</label>
                            <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Aisle / shelf" className={inputCls} />
                        </div>
                        <div>
                            <label className={labelCls}>Branch Selling Price</label>
                            <div className="relative">
                                <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                <input type="number" step="0.01" min="0" value={sellingPrice} onChange={(e) => setSellingPrice(e.target.value)} className={`${inputCls} pl-7`} />
                            </div>
                        </div>
                        <button type="button" onClick={add} disabled={!selectedDrug || submitting}
                            className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 disabled:opacity-50">
                            {submitting ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                            Add to Branch
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

// ─── TransferStockPanel ───────────────────────────────────────────────────────
//
// Atomically moves stock from the current branch to another branch. The backend
// creates two linked StockAdjustment records (source debit + destination credit)
// in a single transaction.
// Backend: POST /inventory/transfer   (requires manage_inventory permission)
//
// Previously there was no UI for inter-branch transfers at all.

function TransferStockPanel({
    item,
    fromBranchId,
    onSuccess,
    onClose,
}: {
    item: BranchInventoryWithDetails;
    fromBranchId: string;
    onSuccess: () => void;
    onClose: () => void;
}) {
    const [branches, setBranches] = useState<BranchListItem[]>([]);
    const [branchesLoading, setBranchesLoading] = useState(true);
    const [toBranchId, setToBranchId] = useState("");
    const [quantity, setQuantity] = useState("");
    const [reason, setReason] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const available = item.available_quantity ?? (item.quantity - item.reserved_quantity);

    useEffect(() => {
        branchApi
            .list({ is_active: true, page_size: 200 })
            .then((r) => setBranches(r.items.filter((b) => b.id !== fromBranchId)))
            .catch(() => setBranches([]))
            .finally(() => setBranchesLoading(false));
    }, [fromBranchId]);

    const qty = parseInt(quantity) || 0;
    const exceedsAvailable = qty > available;
    const canSubmit = qty > 0 && !exceedsAvailable && toBranchId && reason.trim() && !submitting && available > 0;

    const handleSubmit = async () => {
        if (!canSubmit) return;
        setSubmitting(true);
        setError(null);
        try {
            await inventoryApi.transfer({
                from_branch_id: fromBranchId,
                to_branch_id: toBranchId,
                drug_id: item.drug_id,
                quantity: qty,
                reason: reason.trim(),
            });
            onSuccess();
        } catch (err) {
            setError(parseApiError(err));
            setSubmitting(false);
        }
    };

    return (
        <div className="flex flex-col h-full bg-white">
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div className="min-w-0">
                    <h2 className="text-base font-bold text-ink">Transfer Stock</h2>
                    <p className="text-xs text-ink-muted mt-0.5 truncate">{item.drug_name}</p>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 flex-shrink-0 ml-2">
                    <X className="w-4 h-4" />
                </button>
            </div>

            <div className="px-5 py-2.5 bg-slate-50 border-b border-slate-100 flex-shrink-0 space-y-0.5">
                <div>
                    <span className="text-xs text-slate-500">From: </span>
                    <span className="text-xs font-semibold text-ink">{item.branch_name}</span>
                    <span className="text-xs font-mono text-slate-400 ml-1">({item.branch_code})</span>
                </div>
                <div>
                    <span className="text-xs text-slate-500">Available to transfer: </span>
                    <span className={`text-sm font-bold ${available === 0 ? "text-red-600" : "text-ink"}`}>
                        {available.toLocaleString()} units
                    </span>
                </div>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
                {error && (
                    <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />{error}
                    </div>
                )}

                {available === 0 && (
                    <div className="flex items-start gap-2 p-3 rounded-xl bg-amber-50 border border-amber-100 text-xs text-amber-700">
                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                        No available stock to transfer from this branch.
                    </div>
                )}

                {/* Destination branch */}
                <div>
                    <label className={labelCls}>Destination Branch</label>
                    {branchesLoading ? (
                        <div className="h-10 rounded-xl bg-slate-100 animate-pulse" />
                    ) : branches.length === 0 ? (
                        <p className="text-sm text-slate-500 italic py-2">No other active branches available.</p>
                    ) : (
                        <select value={toBranchId} onChange={(e) => setToBranchId(e.target.value)}
                            className={`${inputCls} appearance-none`}>
                            <option value="">Select destination…</option>
                            {branches.map((b) => (
                                <option key={b.id} value={b.id}>{b.name} ({b.code})</option>
                            ))}
                        </select>
                    )}
                </div>

                {/* Quantity */}
                <div>
                    <label className={labelCls}>Quantity</label>
                    <input
                        type="number"
                        min={1}
                        max={available}
                        value={quantity}
                        onChange={(e) => setQuantity(e.target.value.replace(/[^0-9]/g, ""))}
                        placeholder="Enter quantity"
                        className={inputCls}
                    />
                    {exceedsAvailable && (
                        <p className="text-xs text-red-500 mt-1">Exceeds available stock ({available} units)</p>
                    )}
                </div>

                {/* Reason (required by backend) */}
                <div>
                    <label className={labelCls}>Reason <span className="text-red-400">*</span></label>
                    <textarea
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        placeholder="e.g. Restocking branch due to demand surge"
                        rows={3}
                        maxLength={500}
                        className="w-full px-3 py-2.5 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors resize-none placeholder:text-slate-400"
                    />
                </div>
            </div>

            <div className="flex-shrink-0 border-t border-slate-200 px-4 py-4 flex items-center justify-end gap-3">
                <button onClick={onClose} disabled={submitting} type="button"
                    className="px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors">
                    Cancel
                </button>
                <button onClick={handleSubmit} disabled={!canSubmit} type="button"
                    className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2">
                    {submitting
                        ? <><RefreshCw className="w-4 h-4 animate-spin" /> Transferring…</>
                        : <><ArrowRightLeft className="w-4 h-4" /> Transfer Stock</>}
                </button>
            </div>
        </div>
    );
}

// ─── InventoryPage ────────────────────────────────────────────────────────────

export default function InventoryPage() {
    const { activeBranchId } = useAuthStore();
    const [activeView, setActiveView] = useState<ActiveView>("stock");

    // ── Stock view state ──────────────────────────────────────
    const [inventory, setInventory] = useState<BranchInventoryWithDetails[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [searchInput, setSearchInput] = useState("");
    const debouncedSearch = useDebounce(searchInput, 300);
    const [lowStockOnly, setLowStockOnly] = useState(false);
    // FIX: include_zero_stock is now wired to the backend query param
    // Previously this param existed on the API but was never sent from the UI.
    const [includeZeroStock, setIncludeZeroStock] = useState(false);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [total, setTotal] = useState(0);
    const PAGE_SIZE = 25;

    // ── Report state ──────────────────────────────────────────
    const [lowStockItems, setLowStockItems] = useState<LowStockItem[]>([]);
    const [lowStockCounts, setLowStockCounts] = useState({ out: 0, low: 0 });
    const [expiringItems, setExpiringItems] = useState<ExpiringBatchItem[]>([]);
    const [expiringCount, setExpiringCount] = useState(0);
    // FIX: capture total_cost_value / total_selling_value from ExpiringBatchReport
    // for the "value at risk" summary bar — these fields existed on the backend
    // response but were previously discarded.
    const [expiringTotals, setExpiringTotals] = useState<{ cost: number; selling: number } | null>(null);

    // ── Valuation state ───────────────────────────────────────
    const [valuation, setValuation] = useState<InventoryValuationResponse | null>(null);
    const [valuationLoading, setValuationLoading] = useState(false);
    const [valuationError, setValuationError] = useState<string | null>(null);
    const valuationFetchedRef = useRef(false);
    const [valuationPage, setValuationPage] = useState(1);
    const VALUATION_PAGE_SIZE = 25;

    // ── Add batch modal ───────────────────────────────────────
    const [addBatchDrug, setAddBatchDrug] = useState<Drug | null>(null);
    const [addBatchError, setAddBatchError] = useState<string | null>(null);
    const [addBatchLoadingId, setAddBatchLoadingId] = useState<string | null>(null);
    const [addDrugOpen, setAddDrugOpen] = useState(false);

    // ── Side panel (batches / adjust / transfer) ──────────────
    const [sidePanel, setSidePanel] = useState<SidePanel>(null);
    const [panelItem, setPanelItem] = useState<BranchInventoryWithDetails | null>(null);

    // ── Cache freshness tracking ──────────────────────────────
    const [inventoryFromCache, setInventoryFromCache] = useState(false);
    const [inventoryCachedAt, setInventoryCachedAt] = useState<string | undefined>();
    const [lowStockFromCache, setLowStockFromCache] = useState(false);
    const [expiringFromCache, setExpiringFromCache] = useState(false);

    const abortRef = useRef<AbortController | null>(null);

    // Memoize filter parameters to prevent unnecessary re-renders
    const filterKey = useMemo(
        () => JSON.stringify({ debouncedSearch, lowStockOnly, includeZeroStock }),
        [debouncedSearch, lowStockOnly, includeZeroStock]
    );

    // ── Fetch inventory ───────────────────────────────────────
    const fetchInventory = useCallback(async () => {
        if (!activeBranchId) {
            setIsLoading(false);
            return;
        }
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;
        setIsLoading(true);
        setError(null);
        setInventoryFromCache(false);

        try {
            // If offline, go straight to cache
            if (!navigator.onLine || !isBackendReachable()) {
                const result = await localRead.getBranchInventory(
                    activeBranchId,
                    {
                        search: debouncedSearch || undefined,
                        low_stock_only: lowStockOnly || undefined,
                        include_zero_stock: includeZeroStock || undefined,
                    },
                    page,
                    PAGE_SIZE
                );
                if (!controller.signal.aborted) {
                    setInventory(result.items);
                    setTotalPages(result.total_pages);
                    setTotal(result.total);
                    setInventoryFromCache(true);
                    setInventoryCachedAt(new Date().toISOString());
                    void cacheBranchInventoryRows(result.items);
                }
                return;
            }

            // Try server with timeout fallback
            const timeoutResult = await withTimeout(
                // Server fetch
                () => inventoryApi.getBranchInventory(
                    activeBranchId,
                    {
                        page,
                        page_size: PAGE_SIZE,
                        search: debouncedSearch || undefined,
                        low_stock_only: lowStockOnly || undefined,
                        include_zero_stock: includeZeroStock || undefined,
                    },
                    controller.signal
                ),
                // Cache fallback
                () => localRead.getBranchInventory(
                    activeBranchId,
                    {
                        search: debouncedSearch || undefined,
                        low_stock_only: lowStockOnly || undefined,
                        include_zero_stock: includeZeroStock || undefined,
                    },
                    page,
                    PAGE_SIZE
                ),
                { timeoutMs: 15000, dataKey: `inventory:${activeBranchId}:${page}` }
            );

            if (!controller.signal.aborted) {
                setInventory(timeoutResult.data.items);
                setTotalPages(timeoutResult.data.total_pages);
                setTotal(timeoutResult.data.total);
                setInventoryFromCache(timeoutResult.isFromCache);
                setInventoryCachedAt(timeoutResult.cached_at);
                if (!timeoutResult.isFromCache) {
                    void cacheBranchInventoryRows(timeoutResult.data.items);
                }
            }
        } catch (err: unknown) {
            if (err instanceof Error && err.name === "AbortError") return;
            if (!controller.signal.aborted) {
                setError(parseApiError(err));
                setInventoryFromCache(false);
            }
        } finally {
            if (!controller.signal.aborted) setIsLoading(false);
        }
    }, [activeBranchId, page, filterKey]);

    const fetchLowStock = useCallback(async () => {
        if (!activeBranchId) return;
        setLowStockFromCache(false);

        try {
            if (!navigator.onLine || !isBackendReachable()) {
                const result = await localRead.getLowStock(activeBranchId);
                setLowStockItems(result.items);
                setLowStockCounts({ out: result.out_of_stock_count, low: result.low_stock_count });
                setLowStockFromCache(true);
                return;
            }

            const timeoutResult = await withTimeout(
                () => inventoryApi.getLowStock(activeBranchId),
                () => localRead.getLowStock(activeBranchId),
                { timeoutMs: 15000, dataKey: `low_stock:${activeBranchId}` }
            );

            setLowStockItems(timeoutResult.data.items);
            setLowStockCounts({ out: timeoutResult.data.out_of_stock_count, low: timeoutResult.data.low_stock_count });
            setLowStockFromCache(timeoutResult.isFromCache);
        } catch (err: unknown) {
            // Silently fail for secondary reports
            console.debug("Low stock fetch failed:", err);
        }
    }, [activeBranchId]);

    const fetchExpiring = useCallback(async () => {
        if (!activeBranchId) return;
        setExpiringFromCache(false);

        try {
            if (!navigator.onLine || !isBackendReachable()) {
                const result = await localRead.getExpiring(activeBranchId, 90);
                setExpiringItems(result.items);
                setExpiringCount(result.total_items);
                setExpiringTotals({
                    cost: Number(result.total_cost_value),
                    selling: Number(result.total_selling_value),
                });
                setExpiringFromCache(true);
                return;
            }

            const timeoutResult = await withTimeout(
                () => inventoryApi.getExpiring(activeBranchId, 90),
                () => localRead.getExpiring(activeBranchId, 90),
                { timeoutMs: 15000, dataKey: `expiring:${activeBranchId}` }
            );

            setExpiringItems(timeoutResult.data.items);
            setExpiringCount(timeoutResult.data.total_items);
            setExpiringTotals({
                cost: Number(timeoutResult.data.total_cost_value),
                selling: Number(timeoutResult.data.total_selling_value),
            });
            setExpiringFromCache(timeoutResult.isFromCache);
        } catch (err: unknown) {
            // Silently fail for secondary reports
            console.debug("Expiring batch fetch failed:", err);
        }
    }, [activeBranchId]);

    const fetchValuation = useCallback(async () => {
        if (!activeBranchId) return;
        setValuationLoading(true);
        setValuationError(null);
        try {
            if (!navigator.onLine || !isBackendReachable()) {
                const result = await localRead.getValuation(activeBranchId);
                setValuation(result);
                valuationFetchedRef.current = true;
                setValuationPage(1);
                return;
            }
            const result = await inventoryApi.getValuation(activeBranchId);
            setValuation(result);
            valuationFetchedRef.current = true;
            setValuationPage(1);
        } catch (err: unknown) {
            setValuationError(parseApiError(err));
            if (isOfflineError(err)) {
                await fetchValuationOffline();
            }
        } finally {
            setValuationLoading(false);
        }
    }, [activeBranchId]);

    const fetchValuationOffline = useCallback(async () => {
        if (!activeBranchId) return;
        setValuationLoading(true);
        setValuationError(null);
        try {
            const result = await localRead.getValuation(activeBranchId);
            setValuation(result);
            valuationFetchedRef.current = true;
            setValuationPage(1);
        } catch (err: unknown) {
            setValuationError((err as Error)?.message ?? "Failed to load valuation report");
        } finally {
            setValuationLoading(false);
        }
    }, [activeBranchId]);

    useEffect(() => {
        fetchInventory();
        return () => abortRef.current?.abort();
    }, [fetchInventory]);

    useEffect(() => {
        const run = async () => {
            await fetchLowStock();
            await fetchExpiring();
        };
        run();
    }, [fetchLowStock, fetchExpiring]);

    useEffect(() => {
        if (activeView === "valuation" && !valuationFetchedRef.current) fetchValuation();
    }, [activeView, fetchValuation]);

    useAppEvent("inventory:changed", fetchInventory);
    useAppEvent("inventory:changed", fetchLowStock);
    useAppEvent("inventory:changed", fetchExpiring);

    const prevFilters = useRef({ debouncedSearch, lowStockOnly, includeZeroStock });
    useEffect(() => {
        const prev = prevFilters.current;
        if (prev.debouncedSearch !== debouncedSearch || prev.lowStockOnly !== lowStockOnly || prev.includeZeroStock !== includeZeroStock) {
            setPage(1);
            prevFilters.current = { debouncedSearch, lowStockOnly, includeZeroStock };
        }
    }, [debouncedSearch, lowStockOnly, includeZeroStock]);

    // ── Handlers ──────────────────────────────────────────────
    const handleAddStockForItem = async (item: BranchInventoryWithDetails) => {
        setSidePanel(null); // close any open side panel
        setAddBatchError(null);
        setAddBatchLoadingId(item.drug_id);
        try {
            const drug = await drugApi.getById(item.drug_id);
            setAddBatchDrug(drug);
        } catch (err) {
            setAddBatchError(`Could not load drug details: ${parseApiError(err)}`);
        } finally {
            setAddBatchLoadingId(null);
        }
    };

    const handleBatchAdded = (_batch: DrugBatch) => {
        setAddBatchDrug(null);
        valuationFetchedRef.current = false;
        appEvents.emit("inventory:changed");
    };

    const handleBranchDrugAdded = () => {
        setAddDrugOpen(false);
        valuationFetchedRef.current = false;
        setIncludeZeroStock(true);
        appEvents.emit("inventory:changed");
    };

    const openPanel = (panel: SidePanel, item: BranchInventoryWithDetails) => {
        setAddBatchDrug(null);
        setPanelItem(item);
        setSidePanel(panel);
    };

    const closePanel = () => { setSidePanel(null); setPanelItem(null); };

    const handlePanelSuccess = () => {
        closePanel();
        valuationFetchedRef.current = false;
        appEvents.emit("inventory:changed");
    };

    const stockStatusBadge = (item: BranchInventoryWithDetails) => {
        const avail = item.available_quantity ?? (item.quantity - item.reserved_quantity);
        if (avail === 0)
            return <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-50 text-red-600">Out of stock</span>;
        if (avail <= item.drug_reorder_level)
            return <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-amber-50 text-amber-600">Low</span>;
        return <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-50 text-green-600">In stock</span>;
    };

    const VIEWS: { id: ActiveView; label: string; icon: React.ElementType; badge?: number }[] = [
        { id: "stock", label: "All Stock", icon: Package },
        { id: "low_stock", label: "Low Stock", icon: TrendingDown, badge: lowStockCounts.out + lowStockCounts.low },
        { id: "expiring", label: "Expiring Soon", icon: Clock, badge: expiringCount },
        { id: "valuation", label: "Valuation", icon: BarChart3 },
    ];
    const existingDrugIds = new Set(inventory.map((item) => item.drug_id));

    return (
        <div className="flex flex-col h-full bg-surface">

            {/* ── Page header ───────────────────────────────────── */}
            <div className="px-6 py-5 border-b border-slate-200 bg-white flex-shrink-0">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="font-display text-2xl font-bold text-ink">Inventory</h1>
                        <p className="text-sm text-ink-muted mt-0.5">Stock levels and batch tracking for this branch</p>
                    </div>
                    <button
                        onClick={() => {
                            fetchInventory(); fetchLowStock(); fetchExpiring();
                            if (activeView === "valuation") fetchValuation();
                        }}
                        className="w-9 h-9 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                        title="Refresh">
                        <RefreshCw className={`w-4 h-4 ${isLoading || valuationLoading ? "animate-spin" : ""}`} />
                    </button>
                    <button
                        onClick={() => setAddDrugOpen(true)}
                        disabled={!activeBranchId}
                        className="h-9 px-3 rounded-lg inline-flex items-center gap-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title={!activeBranchId ? "Select a branch first" : "Add drugs to this branch"}
                    >
                        <Plus className="w-4 h-4" />
                        Add Drugs
                    </button>
                </div>

                {/* Alert chips */}
                {(lowStockCounts.out > 0 || lowStockCounts.low > 0 || expiringCount > 0) && (
                    <div className="flex gap-2 mt-4 flex-wrap">
                        {lowStockCounts.out > 0 && (
                            <button onClick={() => setActiveView("low_stock")}
                                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-red-50 border border-red-100 hover:bg-red-100 transition-colors">
                                <AlertTriangle className="w-4 h-4 text-red-500" />
                                <span className="text-xs font-semibold text-red-700">{lowStockCounts.out} out of stock</span>
                            </button>
                        )}
                        {lowStockCounts.low > 0 && (
                            <button onClick={() => setActiveView("low_stock")}
                                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-amber-50 border border-amber-100 hover:bg-amber-100 transition-colors">
                                <TrendingDown className="w-4 h-4 text-amber-500" />
                                <span className="text-xs font-semibold text-amber-700">{lowStockCounts.low} low stock</span>
                            </button>
                        )}
                        {expiringCount > 0 && (
                            <button onClick={() => setActiveView("expiring")}
                                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-orange-50 border border-orange-100 hover:bg-orange-100 transition-colors">
                                <Clock className="w-4 h-4 text-orange-500" />
                                <span className="text-xs font-semibold text-orange-700">{expiringCount} expiring within 90 days</span>
                            </button>
                        )}
                    </div>
                )}

                {/* View tabs */}
                <div className="flex gap-1 mt-4 border-b border-slate-100 -mb-5">
                    {VIEWS.map((v) => {
                        const Icon = v.icon;
                        const active = activeView === v.id;
                        return (
                            <button key={v.id} onClick={() => setActiveView(v.id)}
                                className={`flex items-center gap-2 px-4 py-3 text-sm font-semibold border-b-2 transition-all -mb-px ${active ? "border-brand-600 text-brand-600" : "border-transparent text-ink-muted hover:text-ink"}`}>
                                <Icon className="w-4 h-4" />
                                {v.label}
                                {v.badge && v.badge > 0 ? (
                                    <span className={`text-xs px-1.5 py-0.5 rounded-full font-bold ${active ? "bg-brand-100 text-brand-700" : "bg-slate-100 text-ink-muted"}`}>
                                        {v.badge}
                                    </span>
                                ) : null}
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* ── Content + optional side panel ────────────────── */}
            <div className="flex flex-1 min-h-0 overflow-hidden">

                {/* Main content */}
                <div className="flex flex-col flex-1 min-h-0 overflow-hidden">

                    {/* ── ALL STOCK ── */}
                    {activeView === "stock" && (
                        <>
                            <div className="px-6 py-3 border-b border-slate-100 bg-white flex gap-3 items-center flex-shrink-0 flex-wrap">
                                <div className="relative flex-1 max-w-sm">
                                    <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                                    <input value={searchInput} onChange={(e) => setSearchInput(e.target.value)}
                                        placeholder="Search drug name or SKU…"
                                        className="w-full pl-9 pr-9 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500" />
                                    {searchInput && (
                                        <button onClick={() => setSearchInput("")} className="absolute right-2.5 top-2.5 text-ink-muted hover:text-ink">
                                            <X className="w-4 h-4" />
                                        </button>
                                    )}
                                </div>
                                <label className="flex items-center gap-2 text-sm text-ink-secondary cursor-pointer select-none">
                                    <input type="checkbox" checked={lowStockOnly} onChange={(e) => setLowStockOnly(e.target.checked)}
                                        className="w-4 h-4 rounded border-slate-300 text-brand-600" />
                                    Low stock only
                                </label>
                                <label className="flex items-center gap-2 text-sm text-ink-secondary cursor-pointer select-none">
                                    <input type="checkbox" checked={includeZeroStock} onChange={(e) => setIncludeZeroStock(e.target.checked)}
                                        className="w-4 h-4 rounded border-slate-300 text-brand-600" />
                                    Include zero stock
                                </label>
                            </div>

                            {addBatchError && (
                                <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-center flex-shrink-0">
                                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                                    <p className="text-sm text-red-600">{addBatchError}</p>
                                    <button onClick={() => setAddBatchError(null)} className="ml-auto text-ink-muted hover:text-ink"><X className="w-4 h-4" /></button>
                                </div>
                            )}
                            {error && (
                                <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-center flex-shrink-0">
                                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                                    <p className="text-sm text-red-600">{error}</p>
                                    <button onClick={() => setError(null)} className="ml-auto text-ink-muted hover:text-ink"><X className="w-4 h-4" /></button>
                                </div>
                            )}

                            {inventoryFromCache && (
                                <div className="mx-6 mt-3 flex-shrink-0">
                                    <DataFreshnessIndicator
                                        isFromCache={inventoryFromCache}
                                        cached_at={inventoryCachedAt}
                                        compact
                                    />
                                </div>
                            )}

                            <div className="flex-1 overflow-auto">
                                {isLoading ? (
                                    <div className="flex items-center justify-center h-64">
                                        <RefreshCw className="w-6 h-6 text-brand-500 animate-spin" />
                                    </div>
                                ) : !activeBranchId ? (
                                    <div className="flex flex-col items-center justify-center h-64 gap-3 text-ink-muted px-6 text-center">
                                        <Building2 className="w-12 h-12 opacity-20 mb-2" />
                                        <p className="text-base font-semibold text-ink">No branch selected</p>
                                        <p className="text-sm max-w-xs">Please select a branch from the sidebar to view and manage inventory.</p>
                                    </div>
                                ) : inventory.length === 0 ? (
                                    <div className="flex flex-col items-center justify-center h-64 gap-3 text-ink-muted">
                                        <Package className="w-10 h-10 opacity-30" />
                                        <p className="text-sm">No inventory items found</p>
                                        <button onClick={() => setAddDrugOpen(true)}
                                            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700">
                                            <Plus className="w-4 h-4" />
                                            Add Drugs
                                        </button>
                                    </div>
                                ) : (
                                    <table className="w-full text-sm">
                                        <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                                            <tr>
                                                <th className="text-left px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Drug</th>
                                                <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Available</th>
                                                <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Total</th>
                                                <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Location</th>
                                                <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Status</th>
                                                <th className="text-right px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Value</th>
                                                <th className="px-4 py-3 w-[120px]" />
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-slate-100 bg-white">
                                            {inventory.map((item) => {
                                                const available = item.available_quantity ?? (item.quantity - item.reserved_quantity);
                                                const value = available * Number(item.effective_unit_price);
                                                const isThisRowLoading = addBatchLoadingId === item.drug_id;
                                                const isActivePanel = panelItem?.id === item.id;
                                                return (
                                                    <tr key={item.id}
                                                        className={`hover:bg-slate-50/70 transition-colors group ${isActivePanel ? "bg-brand-50/30" : ""}`}>
                                                        <td className="px-6 py-3">
                                                            <div className="font-medium text-ink">{item.drug_name}</div>
                                                            {item.drug_sku && (
                                                                <div className="text-xs font-mono text-ink-muted bg-slate-100 px-1 rounded mt-0.5 inline-block">{item.drug_sku}</div>
                                                            )}
                                                        </td>
                                                        <td className="px-4 py-3 text-center">
                                                            <span className={`font-bold ${available === 0 ? "text-red-600" : available <= item.drug_reorder_level ? "text-amber-600" : "text-ink"}`}>
                                                                {available.toLocaleString()}
                                                            </span>
                                                        </td>
                                                        <td className="px-4 py-3 text-center text-ink-secondary">{item.quantity.toLocaleString()}</td>
                                                        <td className="px-4 py-3 text-ink-muted text-xs">{item.location || "Set location"}</td>
                                                        <td className="px-4 py-3 text-center">{stockStatusBadge(item)}</td>
                                                        <td className="px-6 py-3 text-right">
                                                            <span className="font-medium text-ink">₵{value.toFixed(2)}</span>
                                                        </td>
                                                        {/* Row actions */}
                                                        <td className="px-4 py-3">
                                                            <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                                                                {/* Add batch */}
                                                                <button
                                                                    onClick={() => handleAddStockForItem(item)}
                                                                    disabled={isThisRowLoading}
                                                                    title="Add stock batch"
                                                                    className="p-1.5 rounded-lg hover:bg-emerald-50 text-ink-muted hover:text-emerald-600 disabled:opacity-40 transition-colors">
                                                                    {isThisRowLoading
                                                                        ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                                                                        : <Plus className="w-3.5 h-3.5" />}
                                                                </button>
                                                                {/* View batches (FEFO) */}
                                                                <button
                                                                    onClick={() => openPanel("batches", item)}
                                                                    title="View batches"
                                                                    className={`p-1.5 rounded-lg transition-colors ${sidePanel === "batches" && isActivePanel
                                                                        ? "bg-slate-200 text-ink"
                                                                        : "hover:bg-slate-100 text-ink-muted hover:text-ink"}`}>
                                                                    <Layers className="w-3.5 h-3.5" />
                                                                </button>
                                                                {/* Adjust stock */}
                                                                <button
                                                                    onClick={() => openPanel("adjust", item)}
                                                                    title="Adjust stock"
                                                                    className={`p-1.5 rounded-lg transition-colors ${sidePanel === "adjust" && isActivePanel
                                                                        ? "bg-amber-100 text-amber-700"
                                                                        : "hover:bg-amber-50 text-ink-muted hover:text-amber-600"}`}>
                                                                    <SlidersHorizontal className="w-3.5 h-3.5" />
                                                                </button>
                                                                {/* Transfer to branch */}
                                                                <button
                                                                    onClick={() => openPanel("transfer", item)}
                                                                    title="Transfer to another branch"
                                                                    className={`p-1.5 rounded-lg transition-colors ${sidePanel === "transfer" && isActivePanel
                                                                        ? "bg-blue-100 text-blue-700"
                                                                        : "hover:bg-blue-50 text-ink-muted hover:text-blue-600"}`}>
                                                                    <ArrowRightLeft className="w-3.5 h-3.5" />
                                                                </button>
                                                                <button
                                                                    onClick={() => openPanel("settings", item)}
                                                                    title="Branch settings"
                                                                    className={`p-1.5 rounded-lg transition-colors ${sidePanel === "settings" && isActivePanel
                                                                        ? "bg-slate-200 text-ink"
                                                                        : "hover:bg-slate-100 text-ink-muted hover:text-ink"}`}>
                                                                    <ClipboardEdit className="w-3.5 h-3.5" />
                                                                </button>
                                                            </div>
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                )}
                            </div>

                            {totalPages > 1 && (
                                <div className="px-6 py-3 border-t border-slate-200 bg-white flex items-center justify-between flex-shrink-0">
                                    <p className="text-xs text-ink-muted">{total.toLocaleString()} items · page {page} of {totalPages}</p>
                                    <div className="flex gap-1">
                                        <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}
                                            className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40">
                                            <ChevronLeft className="w-4 h-4" />
                                        </button>
                                        <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                                            className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40">
                                            <ChevronRight className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>
                            )}
                        </>
                    )}

                    {/* ── LOW STOCK ── */}
                    {activeView === "low_stock" && (
                        <div className="flex-1 overflow-auto flex flex-col">
                            {lowStockFromCache && (
                                <div className="px-6 pt-3 flex-shrink-0">
                                    <DataFreshnessIndicator
                                        isFromCache={lowStockFromCache}
                                        compact
                                    />
                                </div>
                            )}
                            <div className="flex-1 overflow-auto">
                            {lowStockItems.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-64 gap-3 text-ink-muted">
                                    <TrendingDown className="w-10 h-10 opacity-30" />
                                    <p className="text-sm text-green-600 font-medium">All stock levels are healthy ✓</p>
                                </div>
                            ) : (
                                <table className="w-full text-sm">
                                    <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                                        <tr>
                                            <th className="text-left px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Drug</th>
                                            {/* FIX: branch_name is now shown — the LowStockReport is org-wide so items
                                                from multiple branches can appear. Previously this field was fetched but
                                                never rendered. */}
                                            <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Branch</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Current Stock</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Reorder Level</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Suggested Order</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Status</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-slate-100 bg-white">
                                        {lowStockItems.map((item) => (
                                            <tr key={item.drug_id} className="hover:bg-slate-50/70 transition-colors">
                                                <td className="px-6 py-3">
                                                    <div className="font-medium text-ink">{item.drug_name}</div>
                                                    {item.sku && <div className="text-xs font-mono text-ink-muted mt-0.5">{item.sku}</div>}
                                                </td>
                                                <td className="px-4 py-3 text-sm text-ink-secondary">{item.branch_name}</td>
                                                <td className="px-4 py-3 text-center">
                                                    <span className={`font-bold text-lg ${item.quantity === 0 ? "text-red-600" : "text-amber-600"}`}>
                                                        {item.quantity}
                                                    </span>
                                                </td>
                                                <td className="px-4 py-3 text-center text-ink-secondary">{item.reorder_level}</td>
                                                <td className="px-4 py-3 text-center text-ink-secondary">{item.recommended_order_quantity}</td>
                                                <td className="px-4 py-3 text-center">
                                                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${item.status === "out_of_stock" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-600"}`}>
                                                        {item.status === "out_of_stock" ? "Out of stock" : "Low stock"}
                                                    </span>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}
                            </div>
                        </div>
                    )}

                    {/* ── EXPIRING SOON ── */}
                    {activeView === "expiring" && (
                        <div className="flex-1 overflow-auto flex flex-col">
                            {expiringFromCache && (
                                <div className="px-6 pt-3 flex-shrink-0">
                                    <DataFreshnessIndicator
                                        isFromCache={expiringFromCache}
                                        compact
                                    />
                                </div>
                            )}
                            <div className="flex-1 overflow-auto">
                            {/* FIX: value-at-risk summary bar — total_cost_value and total_selling_value
                                are returned by ExpiringBatchReport but were previously discarded. */}
                            {expiringTotals && expiringItems.length > 0 && (
                                <div className="px-6 pt-4 pb-1 grid grid-cols-3 gap-3 flex-shrink-0">
                                    {[
                                        { label: "Batches at Risk", value: expiringCount.toString(), color: "text-ink", isCurrency: false },
                                        { label: "Cost Value at Risk", value: expiringTotals.cost, color: "text-amber-600", isCurrency: true },
                                        { label: "Selling Value at Risk", value: expiringTotals.selling, color: "text-red-600", isCurrency: true },
                                    ].map(({ label, value, color, isCurrency }) => (
                                        <div key={label} className="rounded-xl bg-white border border-slate-100 px-4 py-3">
                                            <p className="text-[10px] font-semibold text-ink-muted uppercase tracking-wide">{label}</p>
                                            <p className={`text-xl font-bold mt-1 ${color}`}>
                                                {isCurrency
                                                    ? `₵${Number(value).toLocaleString("en-GH", { minimumFractionDigits: 2 })}`
                                                    : value}
                                            </p>
                                        </div>
                                    ))}
                                </div>
                            )}
                            {expiringItems.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-64 gap-3 text-ink-muted">
                                    <Clock className="w-10 h-10 opacity-30" />
                                    <p className="text-sm text-green-600 font-medium">No batches expiring within 90 days ✓</p>
                                </div>
                            ) : (
                                <table className="w-full text-sm">
                                    <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                                        <tr>
                                            <th className="text-left px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Drug</th>
                                            <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Batch</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Qty Remaining</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Expiry Date</th>
                                            <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Days Left</th>
                                            <th className="text-right px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Value at Risk</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-slate-100 bg-white">
                                        {expiringItems.map((item) => (
                                            <tr key={item.batch_id}
                                                className={`hover:bg-slate-50/70 transition-colors ${item.days_until_expiry <= 30 ? "bg-red-50/30" : ""}`}>
                                                <td className="px-6 py-3 font-medium text-ink">{item.drug_name}</td>
                                                <td className="px-4 py-3">
                                                    <span className="font-mono text-xs bg-slate-100 px-2 py-0.5 rounded">{item.batch_number}</span>
                                                </td>
                                                <td className="px-4 py-3 text-center font-semibold text-ink">{item.remaining_quantity}</td>
                                                <td className="px-4 py-3 text-center text-ink-secondary">
                                                    {new Date(item.expiry_date).toLocaleDateString("en-GH", {
                                                        day: "numeric", month: "short", year: "numeric",
                                                    })}
                                                </td>
                                                <td className="px-4 py-3 text-center">
                                                    <span className={`font-bold text-sm ${item.days_until_expiry <= 30 ? "text-red-600"
                                                        : item.days_until_expiry <= 60 ? "text-amber-600"
                                                            : "text-green-600"}`}>
                                                        {item.days_until_expiry}d
                                                    </span>
                                                </td>
                                                <td className="px-6 py-3 text-right font-medium text-ink">
                                                    ₵{Number(item.selling_value ?? item.cost_value ?? 0).toFixed(2)}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}
                            </div>
                        </div>
                    )}

                    {/* ── VALUATION ── */}
                    {activeView === "valuation" && (
                        <div className="flex-1 overflow-auto">
                            {valuationLoading ? (
                                <div className="flex items-center justify-center h-64">
                                    <RefreshCw className="w-6 h-6 text-brand-500 animate-spin" />
                                </div>
                            ) : valuationError ? (
                                <div className="mx-6 mt-4 rounded-xl bg-red-50 border border-red-100 p-4 flex gap-2 items-start">
                                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium text-red-700">Could not load valuation report</p>
                                        <p className="text-xs text-red-600 mt-0.5">{valuationError}</p>
                                    </div>
                                    <button onClick={fetchValuation} className="ml-auto text-xs font-medium text-red-600 hover:text-red-700 underline underline-offset-2">
                                        Retry
                                    </button>
                                </div>
                            ) : valuation ? (() => {
                                const valTotalPages = Math.max(1, Math.ceil(valuation.items.length / VALUATION_PAGE_SIZE));
                                const valOffset = (valuationPage - 1) * VALUATION_PAGE_SIZE;
                                const valPageItems = valuation.items.slice(valOffset, valOffset + VALUATION_PAGE_SIZE);
                                return (
                                    <>
                                        <div className="px-6 pt-5 pb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                                            {[
                                                { label: "Total Cost Value", value: valuation.total_cost_value, color: "text-ink" },
                                                { label: "Total Selling Value", value: valuation.total_selling_value, color: "text-brand-600" },
                                                { label: "Potential Profit", value: valuation.total_potential_profit, color: "text-emerald-600" },
                                            ].map(({ label, value, color }) => (
                                                <div key={label} className="rounded-xl bg-white border border-slate-100 p-4">
                                                    <p className="text-xs font-medium text-ink-muted uppercase tracking-wide">{label}</p>
                                                    <p className={`text-xl font-bold mt-1 ${color}`}>
                                                        ₵{Number(value).toLocaleString("en-GH", { minimumFractionDigits: 2 })}
                                                    </p>
                                                </div>
                                            ))}
                                            <div className="rounded-xl bg-white border border-slate-100 p-4">
                                                <p className="text-xs font-medium text-ink-muted uppercase tracking-wide">Profit Margin</p>
                                                <p className="text-xl font-bold text-ink mt-1">{Number(valuation.profit_margin_percentage).toFixed(1)}%</p>
                                            </div>
                                        </div>
                                        <table className="w-full text-sm">
                                            <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                                                <tr>
                                                    <th className="text-left px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Drug</th>
                                                    <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Qty</th>
                                                    <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Cost / Unit</th>
                                                    <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Sell / Unit</th>
                                                    <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Total Cost</th>
                                                    <th className="text-right px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Total Sell</th>
                                                    <th className="text-right px-6 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Profit</th>
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-slate-100 bg-white">
                                                {valPageItems.map((item) => (
                                                    <tr key={item.drug_id} className="hover:bg-slate-50/70 transition-colors">
                                                        <td className="px-6 py-3">
                                                            <div className="font-medium text-ink">{item.drug_name}</div>
                                                            {item.sku && <div className="text-xs font-mono text-ink-muted mt-0.5">{item.sku}</div>}
                                                        </td>
                                                        <td className="px-4 py-3 text-center text-ink-secondary">{item.quantity.toLocaleString()}</td>
                                                        <td className="px-4 py-3 text-right text-ink-secondary">₵{Number(item.cost_price).toFixed(2)}</td>
                                                        <td className="px-4 py-3 text-right text-ink-secondary">₵{Number(item.selling_price).toFixed(2)}</td>
                                                        <td className="px-4 py-3 text-right text-ink">₵{Number(item.total_cost_value).toLocaleString("en-GH", { minimumFractionDigits: 2 })}</td>
                                                        <td className="px-4 py-3 text-right font-medium text-brand-600">₵{Number(item.total_selling_value).toLocaleString("en-GH", { minimumFractionDigits: 2 })}</td>
                                                        <td className="px-6 py-3 text-right">
                                                            <span className={`font-semibold ${Number(item.potential_profit) >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                                                                ₵{Number(item.potential_profit).toLocaleString("en-GH", { minimumFractionDigits: 2 })}
                                                            </span>
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                        <div className="px-6 py-3 border-t border-slate-200 bg-white flex items-center justify-between">
                                            <p className="text-xs text-ink-muted">
                                                {valuation.total_items} drugs · {valuation.total_quantity.toLocaleString()} total units ·
                                                as of {new Date(valuation.valuation_date).toLocaleString("en-GH")}
                                                {valTotalPages > 1 && ` · page ${valuationPage} of ${valTotalPages}`}
                                            </p>
                                            {valTotalPages > 1 && (
                                                <div className="flex gap-1">
                                                    <button onClick={() => setValuationPage((p) => Math.max(1, p - 1))} disabled={valuationPage === 1}
                                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40">
                                                        <ChevronLeft className="w-4 h-4" />
                                                    </button>
                                                    <button onClick={() => setValuationPage((p) => Math.min(valTotalPages, p + 1))} disabled={valuationPage === valTotalPages}
                                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted disabled:opacity-40">
                                                        <ChevronRight className="w-4 h-4" />
                                                    </button>
                                                </div>
                                            )}
                                        </div>
                                    </>
                                );
                            })() : null}
                        </div>
                    )}
                </div>

                {/* ── Side panel (slides in beside the table) ─────── */}
                {sidePanel && panelItem && activeBranchId && (
                    <div className="w-[400px] flex-shrink-0 border-l border-slate-200 flex flex-col min-h-0 overflow-hidden bg-white">
                        {sidePanel === "batches" && (
                            <BatchViewerPanel item={panelItem} branchId={activeBranchId} onClose={closePanel} />
                        )}
                        {sidePanel === "adjust" && (
                            <AdjustStockPanel item={panelItem} branchId={activeBranchId} onSuccess={handlePanelSuccess} onClose={closePanel} />
                        )}
                        {sidePanel === "transfer" && (
                            <TransferStockPanel item={panelItem} fromBranchId={activeBranchId} onSuccess={handlePanelSuccess} onClose={closePanel} />
                        )}
                        {sidePanel === "settings" && (
                            <BranchDrugSettingsPanel item={panelItem} branchId={activeBranchId} onSuccess={handlePanelSuccess} onClose={closePanel} />
                        )}
                    </div>
                )}
            </div>

            {/* Add batch modal (full-screen overlay — separate from side panel) */}
            <AnimatePresence>
                {addBatchDrug && activeBranchId && (
                    <AddBatchForm
                        drug={addBatchDrug}
                        branchId={activeBranchId}
                        onSuccess={handleBatchAdded}
                        onCancel={() => setAddBatchDrug(null)}
                    />
                )}
                {addDrugOpen && activeBranchId && (
                    <AddBranchDrugModal
                        branchId={activeBranchId}
                        existingDrugIds={existingDrugIds}
                        onAdded={handleBranchDrugAdded}
                        onClose={() => setAddDrugOpen(false)}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}
