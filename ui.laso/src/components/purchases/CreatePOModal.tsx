/**
 * CreatePOModal.tsx
 * ─────────────────────────────────────────────────────────────
 * Modal for creating a new purchase order.
 *
 * - Supplier dropdown with inline "Add supplier" escape hatch
 * - Dynamic line-item table with drug name search combobox
 * - Shipping cost field
 * - Expected delivery date
 * - Inline field-level validation before submit
 * - Server errors rendered below the form
 */

import { useState, useCallback, useEffect, useRef } from "react";
import {
    X, Plus, Trash2, Package, AlertCircle, ChevronDown, UserPlus,
    Search, Loader2,
} from "lucide-react";
import type { Drug, Supplier, SupplierCreate, PurchaseOrderCreate, PurchaseOrderItemCreate } from "@/types";
import { drugApi } from "@/api/drugs";
import { CreateSupplierModal } from "./CreateSupplierModal";

interface Props {
    branchId: string;
    suppliers: Supplier[];
    suppliersError?: string | null;
    onRetrySuppliers?: () => void;
    onSubmit: (data: PurchaseOrderCreate) => Promise<boolean>;
    onClose: () => void;
    submitting: boolean;
    submitError: string | null;
    onCreateSupplier: (data: SupplierCreate) => Promise<Supplier | null>;
    onSupplierCreated: (supplier: Supplier) => void;
    createSupplierSubmitting: boolean;
    createSupplierError: string | null;
}

interface LineItem {
    _key: string;
    drug_id: string;
    drug_name: string;
    drug_sku: string | null;
    quantity_ordered: number;
    unit_cost: number;
}

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors";

const labelCls = "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

let _keyCounter = 0;
const newKey = () => `item-${++_keyCounter}`;

// ─── Drug Search Combobox ────────────────────────────────────────────────────
// Self-contained per-row combobox: type to search, click to select.
// Once a drug is selected it shows a pill with a clear button.

interface DrugComboboxProps {
    value: string;       // drug_id
    label: string;       // drug_name for display
    onSelect: (drug: Drug) => void;
    onClear: () => void;
    hasError?: boolean;
}

function DrugCombobox({ value, label, onSelect, onClear, hasError }: DrugComboboxProps) {
    const [query, setQuery] = useState("");
    const [results, setResults] = useState<Drug[]>([]);
    const [searching, setSearching] = useState(false);
    const [open, setOpen] = useState(false);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const wrapperRef = useRef<HTMLDivElement>(null);

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, []);

    const search = (q: string) => {
        if (debounceRef.current) clearTimeout(debounceRef.current);
        if (q.trim().length < 2) {
            setResults([]);
            setOpen(false);
            return;
        }
        debounceRef.current = setTimeout(async () => {
            setSearching(true);
            try {
                const res = await drugApi.list({ search: q, page_size: 20, is_active: true });
                setResults(res.items ?? []);
                setOpen(true);
            } catch {
                setResults([]);
            } finally {
                setSearching(false);
            }
        }, 300);
    };

    const handleSelect = (drug: Drug) => {
        onSelect(drug);
        setQuery("");
        setResults([]);
        setOpen(false);
    };

    // ── Selected state: show pill ──────────────────────────────
    if (value) {
        return (
            <div className={`flex items-center gap-2 h-9 px-2.5 rounded-lg border ${hasError ? "border-red-300 bg-red-50/30" : "border-brand-200 bg-brand-50"} text-sm w-full`}>
                <Package className="w-3.5 h-3.5 text-brand-500 flex-shrink-0" />
                <span className="flex-1 truncate text-brand-800 font-medium text-xs">{label}</span>
                <button
                    type="button"
                    onClick={onClear}
                    className="p-0.5 rounded text-brand-400 hover:text-brand-700 hover:bg-brand-100 transition-colors flex-shrink-0"
                    title="Remove drug"
                >
                    <X className="w-3 h-3" />
                </button>
            </div>
        );
    }

    // ── Search state: show input + dropdown ───────────────────
    return (
        <div ref={wrapperRef} className="relative w-full">
            <div className="relative">
                <Search className="absolute left-2.5 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                <input
                    value={query}
                    onChange={(e) => { setQuery(e.target.value); search(e.target.value); }}
                    onFocus={() => { if (results.length > 0) setOpen(true); }}
                    placeholder="Search drug name…"
                    className={`h-9 w-full pl-8 pr-8 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors ${hasError ? "border-red-300 bg-red-50/30" : "border-slate-200"}`}
                />
                {searching && (
                    <Loader2 className="absolute right-2.5 top-2.5 w-3.5 h-3.5 text-slate-400 animate-spin" />
                )}
            </div>

            {/* Dropdown */}
            {open && results.length > 0 && (
                <div className="absolute z-[70] top-full left-0 right-0 mt-1 bg-white border border-slate-200 rounded-xl shadow-lg overflow-hidden max-h-48 overflow-y-auto">
                    {results.map((drug) => (
                        <button
                            key={drug.id}
                            type="button"
                            onMouseDown={(e) => e.preventDefault()} // prevent blur before click
                            onClick={() => handleSelect(drug)}
                            className="w-full flex items-start gap-2.5 px-3 py-2 text-left hover:bg-slate-50 border-b border-slate-50 last:border-0 transition-colors"
                        >
                            <Package className="w-3.5 h-3.5 text-slate-400 flex-shrink-0 mt-0.5" />
                            <div className="min-w-0 flex-1">
                                <p className="text-sm font-semibold text-ink truncate">{drug.name}</p>
                                <p className="text-[10px] text-slate-400 truncate">
                                    {[drug.generic_name, drug.sku, drug.strength]
                                        .filter(Boolean)
                                        .join(" · ")}
                                </p>
                            </div>
                            <span className="text-xs font-semibold text-slate-500 flex-shrink-0 mt-0.5">
                                ₵{drug.unit_price.toFixed(2)}
                            </span>
                        </button>
                    ))}
                </div>
            )}

            {open && query.length >= 2 && results.length === 0 && !searching && (
                <div className="absolute z-[70] top-full left-0 right-0 mt-1 bg-white border border-slate-200 rounded-xl shadow-lg px-3 py-2.5">
                    <p className="text-xs text-slate-400">No drugs found for "{query}"</p>
                </div>
            )}
        </div>
    );
}

// ─── Main Modal ──────────────────────────────────────────────────────────────

export function CreatePOModal({
    branchId,
    suppliersError,
    onRetrySuppliers,
    suppliers,
    onSubmit,
    onClose,
    submitting,
    submitError,
    onCreateSupplier,
    onSupplierCreated,
    createSupplierSubmitting,
    createSupplierError,
}: Props) {
    const [supplierId, setSupplierId] = useState("");
    const [expectedDate, setExpectedDate] = useState("");
    const [shippingCost, setShippingCost] = useState<number>(0);
    const [notes, setNotes] = useState("");
    const [items, setItems] = useState<LineItem[]>([
        { _key: newKey(), drug_id: "", drug_name: "", drug_sku: null, quantity_ordered: 1, unit_cost: 0 },
    ]);
    const [errors, setErrors] = useState<Record<string, string>>({});

    const [showCreateSupplier, setShowCreateSupplier] = useState(false);

    // ── Line item helpers ─────────────────────────────────────

    const addRow = () =>
        setItems((prev) => [
            ...prev,
            { _key: newKey(), drug_id: "", drug_name: "", drug_sku: null, quantity_ordered: 1, unit_cost: 0 },
        ]);

    const removeRow = (key: string) =>
        setItems((prev) => prev.filter((r) => r._key !== key));

    const updateRow = (key: string, patch: Partial<LineItem>) =>
        setItems((prev) =>
            prev.map((r) => (r._key === key ? { ...r, ...patch } : r)),
        );

    const selectDrug = (key: string, drug: Drug) => {
        updateRow(key, {
            drug_id: drug.id,
            drug_name: drug.name,
            drug_sku: drug.sku,
            // Pre-fill unit_cost with the drug's cost_price if available, else unit_price
            unit_cost: drug.cost_price ?? drug.unit_price,
        });
    };

    const clearDrug = (key: string) => {
        updateRow(key, { drug_id: "", drug_name: "", drug_sku: null, unit_cost: 0 });
    };

    // ── Validation ────────────────────────────────────────────

    const validate = (): boolean => {
        const errs: Record<string, string> = {};

        if (!supplierId) errs.supplier = "Select a supplier";
        if (items.some((i) => !i.drug_id))
            errs.items = "All items must have a drug selected";
        if (items.some((i) => i.quantity_ordered < 1))
            errs.items = "Quantity must be at least 1";
        if (items.some((i) => i.unit_cost <= 0))
            errs.items = "Unit cost must be greater than 0";

        const drugIds = items.map((i) => i.drug_id).filter(Boolean);
        if (new Set(drugIds).size !== drugIds.length)
            errs.items = "Duplicate drugs detected — each drug can only appear once";

        setErrors(errs);
        return Object.keys(errs).length === 0;
    };

    // ── Submit ────────────────────────────────────────────────

    const handleSubmit = useCallback(async () => {
        if (!validate()) return;

        const payload: PurchaseOrderCreate = {
            branch_id: branchId,
            supplier_id: supplierId,
            items: items.map(
                (i): PurchaseOrderItemCreate => ({
                    drug_id: i.drug_id,
                    quantity_ordered: i.quantity_ordered,
                    unit_cost: i.unit_cost,
                }),
            ),
            shipping_cost: shippingCost,
            expected_delivery_date: expectedDate || undefined,
            notes: notes || undefined,
        };

        const ok = await onSubmit(payload);
        if (ok) onClose();
    }, [supplierId, items, shippingCost, expectedDate, notes, branchId, onSubmit, onClose]);

    // ── Totals ────────────────────────────────────────────────
    const subtotal = items.reduce((s, i) => s + i.quantity_ordered * i.unit_cost, 0);
    const total = subtotal + shippingCost;

    return (
        <>
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
                <div className="w-full max-w-3xl bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]">

                    {/* Header */}
                    <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 flex-shrink-0">
                        <div>
                            <h2 className="text-lg font-bold text-ink">New Purchase Order</h2>
                            <p className="text-xs text-ink-muted mt-0.5">Create a draft PO to send to your supplier</p>
                        </div>
                        <button
                            onClick={onClose}
                            className="p-2 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>

                    {/* Body */}
                    <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">

                        {/* Server error */}
                        {submitError && (
                            <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600">
                                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                                {submitError}
                            </div>
                        )}

                        {/* Row 1: Supplier + Expected date */}
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <div className="flex items-center justify-between mb-1.5">
                                    <label className={labelCls} style={{ marginBottom: 0 }}>
                                        Supplier *
                                    </label>
                                    <button
                                        type="button"
                                        onClick={() => setShowCreateSupplier(true)}
                                        className="flex items-center gap-1 text-[11px] font-semibold text-brand-600 hover:text-brand-700 transition-colors"
                                    >
                                        <UserPlus className="w-3 h-3" />
                                        New supplier
                                    </button>
                                </div>
                                <div className="relative">
                                    <select
                                        value={supplierId}
                                        onChange={(e) => setSupplierId(e.target.value)}
                                        className={`${inputCls} appearance-none pr-8 ${errors.supplier ? "border-red-300 bg-red-50/30" : ""}`}
                                    >
                                        <option value="">— Select supplier —</option>
                                        {suppliers.map((s) => (
                                            <option key={s.id} value={s.id}>{s.name}</option>
                                        ))}
                                    </select>
                                    <ChevronDown className="absolute right-3 top-3 w-4 h-4 text-slate-400 pointer-events-none" />
                                </div>
                                {errors.supplier && (
                                    <p className="text-xs text-red-500 mt-1">{errors.supplier}</p>
                                )}
                                {suppliersError ? (
                                    <p className="text-xs text-red-500 mt-1 flex items-center gap-1">
                                        <AlertCircle className="w-3 h-3" />
                                        Failed to load suppliers.{" "}
                                        {onRetrySuppliers && (
                                            <button type="button" onClick={onRetrySuppliers} className="underline font-semibold">
                                                Retry
                                            </button>
                                        )}
                                    </p>
                                ) : suppliers.length === 0 && (
                                    <p className="text-xs text-amber-600 mt-1 flex items-center gap-1">
                                        <AlertCircle className="w-3 h-3" />
                                        No suppliers yet — add one above
                                    </p>
                                )}
                            </div>

                            <div>
                                <label className={labelCls}>Expected Delivery</label>
                                <input
                                    type="date"
                                    value={expectedDate}
                                    onChange={(e) => setExpectedDate(e.target.value)}
                                    min={new Date().toISOString().split("T")[0]}
                                    className={inputCls}
                                />
                            </div>
                        </div>

                        {/* Line items table */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label className={labelCls}>Items *</label>
                                <button
                                    onClick={addRow}
                                    type="button"
                                    className="flex items-center gap-1 text-xs font-semibold text-brand-600 hover:text-brand-700 transition-colors"
                                >
                                    <Plus className="w-3.5 h-3.5" /> Add row
                                </button>
                            </div>

                            {errors.items && (
                                <p className="text-xs text-red-500 mb-2 flex items-center gap-1">
                                    <AlertCircle className="w-3.5 h-3.5" />{errors.items}
                                </p>
                            )}

                            <div className="rounded-xl border border-slate-200 overflow-visible">
                                {/* Table header */}
                                <div className="grid grid-cols-[1fr_100px_120px_36px] gap-2 px-3 py-2 bg-slate-50 text-[10px] font-bold text-slate-500 uppercase tracking-widest rounded-t-xl">
                                    <span>Drug</span>
                                    <span>Qty</span>
                                    <span>Unit Cost (₵)</span>
                                    <span />
                                </div>

                                {/* Item rows */}
                                <div className="divide-y divide-slate-100">
                                    {items.map((row) => (
                                        <div
                                            key={row._key}
                                            className="grid grid-cols-[1fr_100px_120px_36px] gap-2 px-3 py-2 items-center"
                                        >
                                            {/* Drug search combobox */}
                                            <DrugCombobox
                                                value={row.drug_id}
                                                label={row.drug_name}
                                                onSelect={(drug) => selectDrug(row._key, drug)}
                                                onClear={() => clearDrug(row._key)}
                                                hasError={!!errors.items && !row.drug_id}
                                            />

                                            <input
                                                type="number"
                                                min={1}
                                                value={row.quantity_ordered}
                                                onChange={(e) =>
                                                    updateRow(row._key, {
                                                        quantity_ordered: Math.max(1, parseInt(e.target.value) || 1),
                                                    })
                                                }
                                                className="h-9 px-2.5 rounded-lg border border-slate-200 text-sm text-center focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500"
                                            />
                                            <input
                                                type="number"
                                                min={0.01}
                                                step={0.01}
                                                value={row.unit_cost || ""}
                                                onChange={(e) =>
                                                    updateRow(row._key, {
                                                        unit_cost: parseFloat(e.target.value) || 0,
                                                    })
                                                }
                                                placeholder="0.00"
                                                className="h-9 px-2.5 rounded-lg border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500"
                                            />
                                            <button
                                                onClick={() => removeRow(row._key)}
                                                disabled={items.length === 1}
                                                type="button"
                                                className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                                            >
                                                <Trash2 className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>

                        {/* Row 2: Shipping + Notes */}
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>Shipping Cost (₵)</label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-slate-400 font-semibold">₵</span>
                                    <input
                                        type="number"
                                        min={0}
                                        step={0.01}
                                        value={shippingCost || ""}
                                        onChange={(e) => setShippingCost(parseFloat(e.target.value) || 0)}
                                        placeholder="0.00"
                                        className={`${inputCls} pl-7`}
                                    />
                                </div>
                            </div>

                            <div>
                                <label className={labelCls}>Notes</label>
                                <input
                                    value={notes}
                                    onChange={(e) => setNotes(e.target.value)}
                                    placeholder="Optional notes…"
                                    className={inputCls}
                                />
                            </div>
                        </div>

                        {/* Totals */}
                        <div className="rounded-xl bg-slate-50 border border-slate-200 p-4 space-y-1.5">
                            <div className="flex justify-between text-xs text-slate-500">
                                <span>Subtotal ({items.length} line{items.length !== 1 ? "s" : ""})</span>
                                <span className="font-medium">₵{subtotal.toFixed(2)}</span>
                            </div>
                            <div className="flex justify-between text-xs text-slate-500">
                                <span>Shipping</span>
                                <span className="font-medium">₵{shippingCost.toFixed(2)}</span>
                            </div>
                            <div className="flex justify-between text-sm font-bold text-ink border-t border-slate-200 pt-2">
                                <span>Total</span>
                                <span>₵{total.toFixed(2)}</span>
                            </div>
                        </div>
                    </div>

                    {/* Footer */}
                    <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-slate-100 flex-shrink-0">
                        <button
                            onClick={onClose}
                            type="button"
                            className="px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleSubmit}
                            disabled={submitting}
                            type="button"
                            className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
                        >
                            {submitting ? (
                                <>
                                    <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                    </svg>
                                    Creating…
                                </>
                            ) : (
                                <>
                                    <Package className="w-4 h-4" />
                                    Create Draft PO
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </div>

            {/* Create Supplier sub-modal */}
            {showCreateSupplier && (
                <CreateSupplierModal
                    onSubmit={onCreateSupplier}
                    onCreated={(newSupplier) => {
                        onSupplierCreated(newSupplier);
                        setSupplierId(newSupplier.id);
                        setShowCreateSupplier(false);
                    }}
                    onClose={() => setShowCreateSupplier(false)}
                    submitting={createSupplierSubmitting}
                    submitError={createSupplierError}
                />
            )}
        </>
    );
}