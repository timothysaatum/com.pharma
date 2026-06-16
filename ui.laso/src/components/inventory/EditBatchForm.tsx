import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import type { Resolver } from "react-hook-form";
import { motion } from "framer-motion";
import { X, Package, AlertTriangle, Loader2, CalendarDays } from "lucide-react";
import { inventoryApi } from "@/api/inventory";
import { isOfflineError, parseApiError } from "@/api/client";
import type { DrugBatch } from "@/types";

const today = new Date().toISOString().split("T")[0];

const editBatchSchema = z.object({
    batch_number: z.string().min(1, "Batch number is required").max(100),
    remaining_quantity: z.coerce.number().int().min(0, "Quantity cannot be negative"),
    manufacturing_date: z.string().optional().or(z.literal("")),
    expiry_date: z
        .string()
        .min(1, "Expiry date is required")
        .refine((d) => d > today, "Expiry date must be in the future"),
    cost_price: z.coerce.number().min(0).optional(),
    selling_price: z.coerce.number().min(0).optional(),
    supplier: z.string().max(255).optional().or(z.literal("")),
});

type EditBatchFormValues = z.infer<typeof editBatchSchema>;

interface EditBatchFormProps {
    batch: DrugBatch;
    drugName: string;
    onSuccess: (batch: DrugBatch) => void;
    onCancel: () => void;
}

export function EditBatchForm({ batch, drugName, onSuccess, onCancel }: EditBatchFormProps) {
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const form = useForm<EditBatchFormValues>({
        resolver: zodResolver(editBatchSchema) as Resolver<EditBatchFormValues>,
        defaultValues: {
            batch_number: batch.batch_number,
            remaining_quantity: batch.remaining_quantity,
            manufacturing_date: batch.manufacturing_date || "",
            expiry_date: batch.expiry_date,
            cost_price: batch.cost_price ?? undefined,
            selling_price: batch.selling_price ?? undefined,
            supplier: batch.supplier ?? "",
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

    const onSubmit = async (values: EditBatchFormValues) => {
        setIsSubmitting(true);
        setError(null);
        try {
            const result = await inventoryApi.updateBatch(batch.id, {
                batch_number: values.batch_number,
                remaining_quantity: values.remaining_quantity,
                manufacturing_date: values.manufacturing_date || undefined,
                expiry_date: values.expiry_date,
                cost_price: values.cost_price,
                selling_price: values.selling_price,
                supplier: values.supplier || undefined,
            });
            onSuccess(result);
        } catch (err) {
            if (isOfflineError(err)) {
                setError("Batch editing is not available in offline mode.");
            } else {
                setError(parseApiError(err));
            }
        } finally {
            setIsSubmitting(false);
        }
    };

    const inputCls = (hasError?: boolean) =>
        `w-full px-3 py-2.5 rounded-xl border text-sm bg-white transition-colors focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 ${hasError ? "border-red-300 bg-red-50/30" : "border-slate-200"
        }`;

    const labelCls = "block text-sm font-medium text-ink mb-1.5";

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
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
                        <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center">
                            <Package className="w-4 h-4 text-white" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">Edit Batch</h2>
                            <p className="text-xs text-ink-muted truncate max-w-xs">{drugName}</p>
                        </div>
                    </div>
                    <button type="button" onClick={onCancel}
                        className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
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
                                <label className={labelCls}>Remaining Qty <span className="text-red-500">*</span></label>
                                <input type="number" min="0" {...register("remaining_quantity")}
                                    className={inputCls(!!errors.remaining_quantity)} />
                                {errors.remaining_quantity && (
                                    <p className="text-xs text-red-500 mt-1">{errors.remaining_quantity.message}</p>
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
                        </div>
                    </div>

                    <div className="px-6 py-4 border-t border-slate-100 flex justify-end gap-2">
                        <button type="button" onClick={onCancel}
                            className="px-4 py-2 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                            Cancel
                        </button>
                        <button type="submit" disabled={isSubmitting}
                            className="px-5 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2">
                            {isSubmitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                            {isSubmitting ? "Saving…" : "Save Changes"}
                        </button>
                    </div>
                </form>
            </motion.div>
        </div>
    );
}
