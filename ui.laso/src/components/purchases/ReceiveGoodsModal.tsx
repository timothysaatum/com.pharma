/**
 * ReceiveGoodsModal.tsx
 * ─────────────────────────────────────────────────────────────
 * Allows a warehouse user to record receipt of goods from an
 * approved (or partially received) purchase order.
 *
 * One row per PO item:
 *  - Shows drug name, ordered/received quantities
 *  - qty to receive now, batch number, manufacturing date, expiry date
 *  - Skips fully-received items (greyed out)
 *  - Validates expiry must be future before submit
 *  - Sends a single ReceivePurchaseOrder payload
 */

import { useState, useCallback } from "react";
import {
    X, CheckCircle2, AlertCircle, Package2,
    Calendar, Hash,
} from "lucide-react";
import type {
    PurchaseOrderWithDetails,
    PurchaseOrderItemWithDetails,
    ReceivePurchaseOrder,
    ReceiveItemData,
} from "@/types";

interface Props {
    po: PurchaseOrderWithDetails;
    onSubmit: (data: ReceivePurchaseOrder) => Promise<boolean>;
    onClose: () => void;
    submitting: boolean;
    submitError: string | null;
}

interface ReceiveRow {
    item: PurchaseOrderItemWithDetails;
    quantity_received: number;
    batch_number: string;
    manufacturing_date: string;
    expiry_date: string;
    included: boolean; // user can un-tick if not receiving this item now
}

const inputCls =
    "w-full h-9 px-2.5 rounded-lg border border-slate-200 text-sm bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors";

export function ReceiveGoodsModal({ po, onSubmit, onClose, submitting, submitError }: Props) {
    const today = new Date().toISOString().split("T")[0];

    const [rows, setRows] = useState<ReceiveRow[]>(() =>
        po.items.map((item) => ({
            item,
            quantity_received: item.remaining_quantity,
            batch_number: "",
            manufacturing_date: "",
            expiry_date: "",
            included: item.remaining_quantity > 0,
        })),
    );

    const [errors, setErrors] = useState<Record<string, string>>({});
    const [receivedDate, setReceivedDate] = useState(today);

    const updateRow = (id: string, patch: Partial<ReceiveRow>) =>
        setRows((prev) => prev.map((r) => (r.item.id === id ? { ...r, ...patch } : r)));

    // ── Submit ────────────────────────────────────────────────
    const handleSubmit = useCallback(async () => {
        // Validate inline so we always read the current `rows` snapshot
        const errs: Record<string, string> = {};
        const included = rows.filter((r) => r.included);

        if (included.length === 0) {
            errs._global = "Select at least one item to receive";
        }
        for (const row of included) {
            const prefix = row.item.id;
            if (row.quantity_received < 1)
                errs[`${prefix}_qty`] = "Must receive at least 1 unit";
            if (row.quantity_received > row.item.remaining_quantity)
                errs[`${prefix}_qty`] = `Max ${row.item.remaining_quantity} unit(s) can be received`;
            if (!row.batch_number.trim())
                errs[`${prefix}_batch`] = "Batch number required";
            if (!row.expiry_date)
                errs[`${prefix}_expiry`] = "Expiry date required";
            else if (row.expiry_date <= today)
                errs[`${prefix}_expiry`] = "Expiry date must be in the future";
        }
        setErrors(errs);
        if (Object.keys(errs).length > 0) return;

        const items: ReceiveItemData[] = rows
            .filter((r) => r.included)
            .map((r) => ({
                purchase_order_item_id: r.item.id,
                quantity_received: r.quantity_received,
                batch_number: r.batch_number.trim(),
                manufacturing_date: r.manufacturing_date || undefined,
                expiry_date: r.expiry_date,
            }));

        const payload: ReceivePurchaseOrder = {
            received_date: receivedDate,
            items,
            notes: undefined,
        };

        const ok = await onSubmit(payload);
        if (ok) onClose();
    }, [rows, receivedDate, onSubmit, onClose]);

    const pendingItems = rows.filter((r) => r.item.remaining_quantity > 0);
    const doneItems = rows.filter((r) => r.item.remaining_quantity === 0);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
            <div className="w-full max-w-3xl bg-white rounded-2xl shadow-2xl flex flex-col max-h-[92vh]">

                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 flex-shrink-0">
                    <div>
                        <h2 className="text-lg font-bold text-ink">Receive Goods</h2>
                        <p className="text-xs text-ink-muted mt-0.5">
                            PO {po.po_number} · {po.supplier_name}
                        </p>
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

                    {errors._global && (
                        <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-50 border border-amber-200 text-sm text-amber-700">
                            <AlertCircle className="w-4 h-4 flex-shrink-0" />
                            {errors._global}
                        </div>
                    )}

                    {/* Received date */}
                    <div className="flex items-center gap-4">
                        <div className="flex-1 max-w-xs">
                            <label className="block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5">
                                Date Received
                            </label>
                            <input
                                type="date"
                                value={receivedDate}
                                max={today}
                                onChange={(e) => setReceivedDate(e.target.value)}
                                className={inputCls}
                            />
                        </div>
                        <div className="text-sm text-slate-500 pt-5">
                            {po.receipt_progress}
                        </div>
                    </div>

                    {/* Pending items */}
                    {pendingItems.length > 0 && (
                        <div className="space-y-3">
                            <h3 className="text-[11px] font-bold text-ink-muted uppercase tracking-widest">
                                Items to receive
                            </h3>
                            {pendingItems.map((row) => {
                                const p = row.item.id;
                                return (
                                    <div
                                        key={p}
                                        className={`rounded-2xl border p-4 space-y-3 transition-colors ${row.included
                                            ? "border-slate-200 bg-white"
                                            : "border-slate-100 bg-slate-50 opacity-60"
                                            }`}
                                    >
                                        {/* Drug header + include toggle */}
                                        <div className="flex items-start justify-between gap-2">
                                            <div className="flex items-center gap-2.5 min-w-0">
                                                <input
                                                    type="checkbox"
                                                    checked={row.included}
                                                    onChange={(e) =>
                                                        updateRow(p, { included: e.target.checked })
                                                    }
                                                    className="w-4 h-4 rounded accent-brand-600 flex-shrink-0"
                                                />
                                                <div className="min-w-0">
                                                    <p className="text-sm font-bold text-ink truncate">
                                                        {row.item.drug_name}
                                                    </p>
                                                    <p className="text-xs text-slate-500">
                                                        {row.item.drug_generic_name && `${row.item.drug_generic_name} · `}
                                                        Ordered: {row.item.quantity_ordered} ·
                                                        Received: {row.item.quantity_received} ·
                                                        <span className="font-semibold text-brand-600"> Remaining: {row.item.remaining_quantity}</span>
                                                    </p>
                                                </div>
                                            </div>
                                        </div>

                                        {/* Form fields — only when included */}
                                        {row.included && (
                                            <div className="grid grid-cols-2 gap-3 pl-6">
                                                {/* Qty */}
                                                <div>
                                                    <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
                                                        Qty Receiving *
                                                    </label>
                                                    <input
                                                        type="number"
                                                        min={1}
                                                        max={row.item.remaining_quantity}
                                                        value={row.quantity_received}
                                                        onChange={(e) =>
                                                            updateRow(p, {
                                                                quantity_received: parseInt(e.target.value) || 0,
                                                            })
                                                        }
                                                        className={`${inputCls} ${errors[`${p}_qty`] ? "border-red-300 bg-red-50/30" : ""}`}
                                                    />
                                                    {errors[`${p}_qty`] && (
                                                        <p className="text-xs text-red-500 mt-1">{errors[`${p}_qty`]}</p>
                                                    )}
                                                </div>

                                                {/* Batch number */}
                                                <div>
                                                    <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
                                                        Batch Number *
                                                    </label>
                                                    <div className="relative">
                                                        <Hash className="absolute left-2.5 top-2 w-3.5 h-3.5 text-slate-400" />
                                                        <input
                                                            value={row.batch_number}
                                                            onChange={(e) =>
                                                                updateRow(p, { batch_number: e.target.value })
                                                            }
                                                            placeholder="e.g. LOT-2026-001"
                                                            className={`${inputCls} pl-8 ${errors[`${p}_batch`] ? "border-red-300 bg-red-50/30" : ""}`}
                                                        />
                                                    </div>
                                                    {errors[`${p}_batch`] && (
                                                        <p className="text-xs text-red-500 mt-1">{errors[`${p}_batch`]}</p>
                                                    )}
                                                </div>

                                                {/* Mfg date */}
                                                <div>
                                                    <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
                                                        Manufacturing Date
                                                    </label>
                                                    <div className="relative">
                                                        <Calendar className="absolute left-2.5 top-2 w-3.5 h-3.5 text-slate-400" />
                                                        <input
                                                            type="date"
                                                            value={row.manufacturing_date}
                                                            max={today}
                                                            onChange={(e) =>
                                                                updateRow(p, { manufacturing_date: e.target.value })
                                                            }
                                                            className={`${inputCls} pl-8`}
                                                        />
                                                    </div>
                                                </div>

                                                {/* Expiry date */}
                                                <div>
                                                    <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
                                                        Expiry Date *
                                                    </label>
                                                    <div className="relative">
                                                        <Calendar className="absolute left-2.5 top-2 w-3.5 h-3.5 text-slate-400" />
                                                        <input
                                                            type="date"
                                                            value={row.expiry_date}
                                                            min={tomorrow()}
                                                            onChange={(e) =>
                                                                updateRow(p, { expiry_date: e.target.value })
                                                            }
                                                            className={`${inputCls} pl-8 ${errors[`${p}_expiry`] ? "border-red-300 bg-red-50/30" : ""}`}
                                                        />
                                                    </div>
                                                    {errors[`${p}_expiry`] && (
                                                        <p className="text-xs text-red-500 mt-1">{errors[`${p}_expiry`]}</p>
                                                    )}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    {/* Already-received items */}
                    {doneItems.length > 0 && (
                        <div className="space-y-2">
                            <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">
                                Already received
                            </h3>
                            {doneItems.map((row) => (
                                <div
                                    key={row.item.id}
                                    className="flex items-center gap-3 rounded-xl border border-slate-100 bg-slate-50 px-4 py-2.5 opacity-70"
                                >
                                    <CheckCircle2 className="w-4 h-4 text-green-500 flex-shrink-0" />
                                    <div className="min-w-0">
                                        <p className="text-sm font-semibold text-ink-muted truncate">
                                            {row.item.drug_name}
                                        </p>
                                        <p className="text-xs text-slate-400">
                                            {row.item.quantity_ordered} / {row.item.quantity_ordered} received
                                            {row.item.batch_number && ` · Batch ${row.item.batch_number}`}
                                        </p>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
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
                        disabled={submitting || pendingItems.length === 0}
                        type="button"
                        className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
                    >
                        {submitting ? (
                            <>
                                <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                </svg>
                                Saving…
                            </>
                        ) : (
                            <>
                                <Package2 className="w-4 h-4" />
                                Confirm Receipt
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
}

function tomorrow(): string {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return d.toISOString().split("T")[0];
}