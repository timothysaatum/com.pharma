import { useState, useEffect, useRef } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import type { Resolver } from "react-hook-form";
import { motion, AnimatePresence } from "framer-motion";
import { X, Package, AlertTriangle, Loader2, CalendarDays, ChevronDown, Search, ShoppingCart } from "lucide-react";
import { inventoryApi } from "@/api/inventory";
import { get, parseApiError } from "@/api/client";
import type { Drug, DrugBatch, PurchaseOrderResponse, PaginatedResponse } from "@/types";

const today = new Date().toISOString().split("T")[0];

const batchSchema = z.object({
    batch_number: z.string().min(1, "Batch number is required").max(100),
    quantity: z.coerce.number().int().min(1, "Quantity must be at least 1"),
    manufacturing_date: z.string().optional().or(z.literal("")),
    expiry_date: z
        .string()
        .min(1, "Expiry date is required")
        .refine((d) => d > today, "Expiry date must be in the future"),
    cost_price: z.coerce.number().min(0).optional(),
    selling_price: z.coerce.number().min(0).optional(),
    supplier: z.string().max(255).optional().or(z.literal("")),
    purchase_order_id: z
        .string()
        .refine(
            (v) => !v || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v),
            "Must be a valid UUID (e.g. copy from a Purchase Order)"
        )
        .optional()
        .or(z.literal("")),
});

type BatchFormValues = z.infer<typeof batchSchema>;

interface AddBatchFormProps {
    drug: Drug;
    branchId: string;
    onSuccess: (batch: DrugBatch) => void;
    onCancel: () => void;
}

// ── Minimal PO shape we need for the picker ──────────────────────────────────
// PurchaseOrderResponse from @/types has all these fields; we just alias the
// two statuses that are valid for a goods-receipt linkage.
type ReceivablePO = Pick<
    PurchaseOrderResponse,
    "id" | "po_number" | "status" | "total_amount" | "expected_delivery_date"
>;

export function AddBatchForm({ drug, branchId, onSuccess, onCancel }: AddBatchFormProps) {
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // ── Purchase order picker ─────────────────────────────────────────────────
    const [poList, setPoList] = useState<ReceivablePO[]>([]);
    const [poLoading, setPoLoading] = useState(true);
    const [poError, setPoError] = useState<string | null>(null);
    const [poOpen, setPoOpen] = useState(false);
    const [poSearch, setPoSearch] = useState("");
    const [selectedPo, setSelectedPo] = useState<ReceivablePO | null>(null);
    const poDropdownRef = useRef<HTMLDivElement>(null);
    const poSearchRef = useRef<HTMLInputElement>(null);

    // Fetch approved + ordered POs for this branch in parallel on mount.
    // Only these two statuses make sense as a goods-receipt source.
    useEffect(() => {
        let cancelled = false;
        const fetchPOs = async () => {
            setPoLoading(true);
            setPoError(null);
            try {
                const qs = (status: string) =>
                    `/purchase-orders?branch_id=${branchId}&status=${status}&page_size=100`;
                const [approved, ordered] = await Promise.all([
                    get<PaginatedResponse<PurchaseOrderResponse>>(qs("approved")),
                    get<PaginatedResponse<PurchaseOrderResponse>>(qs("ordered")),
                ]);
                if (!cancelled) {
                    // Merge and sort newest first by PO number (lexicographic desc)
                    const merged = [...approved.items, ...ordered.items].sort((a, b) =>
                        b.po_number.localeCompare(a.po_number)
                    );
                    setPoList(merged);
                }
            } catch (err) {
                if (!cancelled) setPoError(parseApiError(err));
            } finally {
                if (!cancelled) setPoLoading(false);
            }
        };
        fetchPOs();
        return () => { cancelled = true; };
    }, [branchId]);

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (poDropdownRef.current && !poDropdownRef.current.contains(e.target as Node)) {
                setPoOpen(false);
            }
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, []);

    // Focus the search input when dropdown opens
    useEffect(() => {
        if (poOpen) {
            setTimeout(() => poSearchRef.current?.focus(), 30);
        } else {
            setPoSearch("");
        }
    }, [poOpen]);

    const form = useForm<BatchFormValues>({
        resolver: zodResolver(batchSchema) as Resolver<BatchFormValues>,
        defaultValues: {
            batch_number: "",
            quantity: 1,
            expiry_date: "",
            // unit_price/cost_price are number in index.ts — no parseFloat needed
            cost_price: drug.cost_price ?? undefined,
            selling_price: drug.unit_price,
            supplier: drug.supplier ?? "",
        },
    });

    const { register, handleSubmit, watch, formState: { errors } } = form;

    const expiryDate = watch("expiry_date");
    const daysUntilExpiry = expiryDate
        ? Math.floor((new Date(expiryDate).getTime() - Date.now()) / 86_400_000)
        : null;

    const expiryColor =
        daysUntilExpiry === null
            ? ""
            : daysUntilExpiry <= 30
                ? "text-red-600"
                : daysUntilExpiry <= 90
                    ? "text-amber-600"
                    : "text-green-600";

    // Helper: set both the display state and the hidden form field atomically
    const selectPo = (po: ReceivablePO | null) => {
        setSelectedPo(po);
        form.setValue("purchase_order_id", po?.id ?? "");
        form.clearErrors("purchase_order_id");
        setPoOpen(false);
    };

    const filteredPos = poList.filter((po) =>
        po.po_number.toLowerCase().includes(poSearch.toLowerCase())
    );

    const onSubmit = async (values: BatchFormValues) => {
        setIsSubmitting(true);
        setError(null);
        try {
            const result = await inventoryApi.createBatch({
                branch_id: branchId,
                drug_id: drug.id,
                batch_number: values.batch_number,
                quantity: values.quantity,
                // remaining_quantity omitted — server always sets it equal to quantity
                manufacturing_date: values.manufacturing_date || undefined,
                expiry_date: values.expiry_date,
                cost_price: values.cost_price,
                selling_price: values.selling_price,
                supplier: values.supplier || undefined,
                purchase_order_id: values.purchase_order_id || undefined,
            });
            onSuccess(result);
        } catch (err) {
            setError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    const inputCls = (hasError?: boolean) =>
        `w-full px-3 py-2.5 rounded-xl border text-sm bg-white transition-colors focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 ${hasError ? "border-red-300 bg-red-50/30" : "border-slate-200"
        }`;

    const labelCls = "block text-sm font-medium text-ink mb-1.5";

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
            <motion.div
                initial={{ opacity: 0, scale: 0.96, y: 16 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96, y: 16 }}
                transition={{ duration: 0.2 }}
                className="w-full max-w-lg bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]"
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-emerald-600 flex items-center justify-center">
                            <Package className="w-4 h-4 text-white" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">Add Stock Batch</h2>
                            <p className="text-xs text-ink-muted truncate max-w-xs">{drug.name}</p>
                        </div>
                    </div>
                    <button type="button" onClick={onCancel}
                        className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Drug summary card */}
                <div className="mx-6 mt-4 rounded-xl bg-slate-50 border border-slate-100 p-3 flex items-center gap-3">
                    <div className="flex-1 min-w-0">
                        <p className="text-sm font-semibold text-ink truncate">{drug.name}</p>
                        <p className="text-xs text-ink-muted">
                            {[drug.strength, drug.dosage_form].filter(Boolean).join(" · ")}
                        </p>
                    </div>
                    <div className="text-right flex-shrink-0">
                        <p className="text-xs text-ink-muted">Unit Price</p>
                        <p className="text-sm font-bold text-ink">₵{drug.unit_price.toFixed(2)}</p>
                    </div>
                </div>

                {error && (
                    <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-start">
                        <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                        <p className="text-sm text-red-600">{error}</p>
                    </div>
                )}

                <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col flex-1 overflow-hidden">
                    <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
                        <div className="grid grid-cols-2 gap-4">
                            <div className="col-span-2">
                                <label className={labelCls}>Batch Number <span className="text-red-500">*</span></label>
                                <input {...register("batch_number")} placeholder="e.g. BATCH-2025-001"
                                    className={inputCls(!!errors.batch_number)} />
                                {errors.batch_number && (
                                    <p className="text-xs text-red-500 mt-1">{errors.batch_number.message}</p>
                                )}
                            </div>

                            <div>
                                <label className={labelCls}>Quantity Received <span className="text-red-500">*</span></label>
                                <input type="number" min="1" {...register("quantity")}
                                    className={inputCls(!!errors.quantity)} />
                                {errors.quantity && (
                                    <p className="text-xs text-red-500 mt-1">{errors.quantity.message}</p>
                                )}
                            </div>

                            <div>
                                <label className={labelCls}>Supplier</label>
                                <input {...register("supplier")} placeholder="Supplier name" className={inputCls()} />
                            </div>

                            <div>
                                <label className={labelCls}>Manufacturing Date</label>
                                <div className="relative">
                                    <input type="date" {...register("manufacturing_date")} max={today} className={inputCls()} />
                                    <CalendarDays className="absolute right-3 top-3 w-4 h-4 text-ink-muted pointer-events-none" />
                                </div>
                            </div>

                            <div>
                                <label className={labelCls}>Expiry Date <span className="text-red-500">*</span></label>
                                <div className="relative">
                                    <input type="date" {...register("expiry_date")} min={today}
                                        className={inputCls(!!errors.expiry_date)} />
                                    <CalendarDays className="absolute right-3 top-3 w-4 h-4 text-ink-muted pointer-events-none" />
                                </div>
                                {errors.expiry_date && (
                                    <p className="text-xs text-red-500 mt-1">{errors.expiry_date.message}</p>
                                )}
                                {daysUntilExpiry !== null && !errors.expiry_date && (
                                    <p className={`text-xs mt-1 font-medium ${expiryColor}`}>
                                        {daysUntilExpiry} days until expiry{daysUntilExpiry <= 90 && " ⚠️"}
                                    </p>
                                )}
                            </div>

                            <div>
                                <label className={labelCls}>Cost Price (GHS)</label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                    <input type="number" step="0.01" min="0" {...register("cost_price")}
                                        placeholder="0.00" className={`${inputCls()} pl-7`} />
                                </div>
                            </div>

                            <div>
                                <label className={labelCls}>Selling Price (GHS)</label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                    <input type="number" step="0.01" min="0" {...register("selling_price")}
                                        placeholder="0.00" className={`${inputCls()} pl-7`} />
                                </div>
                            </div>

                            <div className="col-span-2">
                                <label className={labelCls}>
                                    Purchase Order{" "}
                                    <span className="font-normal text-ink-muted">(optional)</span>
                                </label>

                                {/* Hidden field keeps the UUID in the form state */}
                                <input type="hidden" {...register("purchase_order_id")} />

                                {poError ? (
                                    <div className="flex items-center gap-2 px-3 py-2 rounded-xl border border-amber-200 bg-amber-50 text-xs text-amber-700">
                                        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                                        Could not load purchase orders — you can continue without linking one.
                                    </div>
                                ) : (
                                    <div ref={poDropdownRef} className="relative">
                                        {/* Trigger button */}
                                        <button
                                            type="button"
                                            onClick={() => setPoOpen((o) => !o)}
                                            className={`w-full flex items-center justify-between px-3 py-2.5 rounded-xl border text-sm bg-white transition-colors focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 ${errors.purchase_order_id
                                                ? "border-red-300 bg-red-50/30"
                                                : "border-slate-200"
                                                }`}
                                        >
                                            {poLoading ? (
                                                <span className="flex items-center gap-2 text-ink-muted">
                                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                    Loading purchase orders…
                                                </span>
                                            ) : selectedPo ? (
                                                <span className="flex items-center gap-2 min-w-0">
                                                    <ShoppingCart className="w-3.5 h-3.5 text-brand-500 flex-shrink-0" />
                                                    <span className="font-mono font-semibold text-ink truncate">
                                                        {selectedPo.po_number}
                                                    </span>
                                                    <span className={`text-xs px-1.5 py-0.5 rounded-full font-semibold flex-shrink-0 ${selectedPo.status === "approved"
                                                        ? "bg-emerald-50 text-emerald-700"
                                                        : "bg-blue-50 text-blue-700"
                                                        }`}>
                                                        {selectedPo.status}
                                                    </span>
                                                </span>
                                            ) : poList.length === 0 ? (
                                                <span className="text-ink-muted">No approved or ordered POs available</span>
                                            ) : (
                                                <span className="text-ink-muted">Select a purchase order…</span>
                                            )}
                                            <span className="flex items-center gap-1 flex-shrink-0 ml-2">
                                                {selectedPo && (
                                                    <span
                                                        role="button"
                                                        tabIndex={0}
                                                        onClick={(e) => { e.stopPropagation(); selectPo(null); }}
                                                        onKeyDown={(e) => e.key === "Enter" && (e.stopPropagation(), selectPo(null))}
                                                        className="w-4 h-4 rounded flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100"
                                                    >
                                                        <X className="w-3 h-3" />
                                                    </span>
                                                )}
                                                <ChevronDown className={`w-4 h-4 text-ink-muted transition-transform duration-150 ${poOpen ? "rotate-180" : ""}`} />
                                            </span>
                                        </button>

                                        {/* Dropdown panel */}
                                        <AnimatePresence>
                                            {poOpen && !poLoading && (
                                                <motion.div
                                                    initial={{ opacity: 0, y: -4, scale: 0.98 }}
                                                    animate={{ opacity: 1, y: 0, scale: 1 }}
                                                    exit={{ opacity: 0, y: -4, scale: 0.98 }}
                                                    transition={{ duration: 0.12 }}
                                                    className="absolute z-50 top-full mt-1.5 left-0 right-0 bg-white rounded-xl border border-slate-200 shadow-xl overflow-hidden"
                                                >
                                                    {/* Search */}
                                                    <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-100">
                                                        <Search className="w-3.5 h-3.5 text-ink-muted flex-shrink-0" />
                                                        <input
                                                            ref={poSearchRef}
                                                            value={poSearch}
                                                            onChange={(e) => setPoSearch(e.target.value)}
                                                            placeholder="Search by PO number…"
                                                            className="flex-1 text-sm bg-transparent focus:outline-none placeholder:text-ink-muted"
                                                        />
                                                        {poSearch && (
                                                            <button
                                                                type="button"
                                                                onClick={() => setPoSearch("")}
                                                                className="text-ink-muted hover:text-ink">
                                                                <X className="w-3.5 h-3.5" />
                                                            </button>
                                                        )}
                                                    </div>

                                                    {/* Options list */}
                                                    <div className="max-h-48 overflow-y-auto">
                                                        {filteredPos.length === 0 ? (
                                                            <p className="px-3 py-4 text-sm text-ink-muted text-center">
                                                                {poSearch ? "No matching purchase orders" : "No approved or ordered purchase orders for this branch"}
                                                            </p>
                                                        ) : (
                                                            filteredPos.map((po) => (
                                                                <button
                                                                    key={po.id}
                                                                    type="button"
                                                                    onClick={() => selectPo(po)}
                                                                    className={`w-full flex items-center justify-between px-3 py-2.5 text-sm text-left hover:bg-slate-50 transition-colors ${selectedPo?.id === po.id ? "bg-brand-50" : ""
                                                                        }`}
                                                                >
                                                                    <span className="flex items-center gap-2 min-w-0">
                                                                        <ShoppingCart className={`w-3.5 h-3.5 flex-shrink-0 ${selectedPo?.id === po.id ? "text-brand-500" : "text-ink-muted"}`} />
                                                                        <span className="font-mono font-semibold text-ink truncate">
                                                                            {po.po_number}
                                                                        </span>
                                                                        <span className={`text-xs px-1.5 py-0.5 rounded-full font-semibold flex-shrink-0 ${po.status === "approved"
                                                                            ? "bg-emerald-50 text-emerald-700"
                                                                            : "bg-blue-50 text-blue-700"
                                                                            }`}>
                                                                            {po.status}
                                                                        </span>
                                                                    </span>
                                                                    <span className="text-right flex-shrink-0 ml-3">
                                                                        {po.expected_delivery_date && (
                                                                            <span className="block text-xs text-ink-muted">
                                                                                Due {new Date(po.expected_delivery_date).toLocaleDateString("en-GH", {
                                                                                    day: "numeric", month: "short",
                                                                                })}
                                                                            </span>
                                                                        )}
                                                                        <span className="block text-xs font-medium text-ink">
                                                                            ₵{Number(po.total_amount).toFixed(2)}
                                                                        </span>
                                                                    </span>
                                                                </button>
                                                            ))
                                                        )}
                                                    </div>
                                                </motion.div>
                                            )}
                                        </AnimatePresence>
                                    </div>
                                )}
                                {errors.purchase_order_id && (
                                    <p className="text-xs text-red-500 mt-1">{errors.purchase_order_id.message}</p>
                                )}
                            </div>
                        </div>
                    </div>

                    <div className="px-6 py-4 border-t border-slate-100 flex justify-end gap-2">
                        <button type="button" onClick={onCancel}
                            className="px-4 py-2 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                            Cancel
                        </button>
                        <button type="submit" disabled={isSubmitting}
                            className="px-5 py-2 text-sm font-semibold text-white bg-emerald-600 hover:bg-emerald-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2">
                            {isSubmitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                            {isSubmitting ? "Adding stock…" : "Add Batch"}
                        </button>
                    </div>
                </form>
            </motion.div>
        </div>
    );
}