/**
 * PurchaseOrderDetailPanel.tsx
 * ─────────────────────────────────────────────────────────────
 * Slide-over panel showing full PO detail and all workflow actions.
 *
 * Renders inside the PurchasesPage beside the list.
 * Handles: submit, approve, reject (with reason modal), receive goods.
 */

import { useState } from "react";
import {
    X, CheckCircle2, XCircle, Send,
    Package2, AlertCircle, Clock,
    Building2, User2, CalendarDays, Truck,
} from "lucide-react";
import { POStatusBadge } from "./POStatusBadge";
import { ReceiveGoodsModal } from "./ReceiveGoodsModal";
import type { PurchaseOrderWithDetails, PurchaseOrderItemWithDetails, User } from "@/types";
import { useAuthStore } from "@/stores/authStore";
// Python Decimal serialises to a JSON string — always coerce with Number()
// before calling .toFixed() so the panel never crashes on string values.
function fmtGHS(value: number | string | null | undefined): string {
    return `₵${Number(value ?? 0).toFixed(2)}`;
}

// ─── Permission helper ────────────────────────────────────────────────────────
//
// UserPermissions = { additional: string[], denied: string[] }
//
// Roles that can approve purchase orders by default:
//   super_admin, admin, manager
//
// A user can also approve if "approve_purchase_orders" is in their
// `additional` list, UNLESS it is explicitly in their `denied` list.
//
const APPROVER_ROLES = new Set(["super_admin", "admin", "manager"]);
const APPROVE_PERMISSION = "approve_purchase_orders";

function hasApprovePermission(user: User | null): boolean {
    if (!user) return false;

    const denied = user.permissions?.denied ?? [];
    if (denied.includes(APPROVE_PERMISSION)) return false;

    // Role-based access
    if (APPROVER_ROLES.has(user.role)) return true;

    // Explicit grant via additional permissions
    const additional = user.permissions?.additional ?? [];
    return additional.includes(APPROVE_PERMISSION);
}

// ─────────────────────────────────────────────────────────────────────────────

interface Props {
    po: PurchaseOrderWithDetails;
    onClose: () => void;
    // Actions — wired from PurchasesPage
    onSubmit: (id: string) => Promise<unknown>;
    onApprove: (id: string) => Promise<unknown>;
    onReject: (id: string, reason: string) => Promise<unknown>;
    onCancel: (id: string, reason: string) => Promise<unknown>;
    onReceive: (data: import("@/types").ReceivePurchaseOrder) => Promise<boolean>;
    actionLoading: boolean;
    actionError: string | null;
    receiveSubmitting: boolean;
    receiveError: string | null;
}

function DetailRow({
    icon: Icon,
    label,
    value,
}: {
    icon: React.ElementType;
    label: string;
    value: React.ReactNode;
}) {
    return (
        <div className="flex items-start gap-3">
            <Icon className="w-4 h-4 text-slate-400 mt-0.5 flex-shrink-0" />
            <div>
                <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">{label}</p>
                <p className="text-sm font-semibold text-ink mt-0.5">{value ?? "—"}</p>
            </div>
        </div>
    );
}

function ItemRow({ item }: { item: PurchaseOrderItemWithDetails }) {
    const pct = item.quantity_ordered > 0
        ? Math.min(100, (item.quantity_received / item.quantity_ordered) * 100)
        : 0;

    return (
        <div className="px-4 py-3 space-y-1.5">
            <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                    <p className="text-sm font-semibold text-ink truncate">{item.drug_name}</p>
                    {item.drug_generic_name && (
                        <p className="text-xs text-slate-400">{item.drug_generic_name}</p>
                    )}
                </div>
                <div className="text-right flex-shrink-0">
                    <p className="text-sm font-bold text-ink">{fmtGHS(item.total_cost)}</p>
                    <p className="text-xs text-slate-400">{fmtGHS(item.unit_cost)} ea</p>
                </div>
            </div>

            <div className="flex items-center gap-3">
                {/* Progress bar */}
                <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                    <div
                        className={`h-full rounded-full transition-all ${item.is_fully_received ? "bg-green-500" : "bg-brand-500"
                            }`}
                        style={{ width: `${pct}%` }}
                    />
                </div>
                <p className="text-xs text-slate-500 flex-shrink-0 w-24 text-right">
                    {item.quantity_received} / {item.quantity_ordered} received
                </p>
            </div>

            {item.batch_number && (
                <p className="text-xs text-slate-400">
                    Batch: {item.batch_number}
                    {item.expiry_date && ` · Exp: ${item.expiry_date}`}
                </p>
            )}
        </div>
    );
}

export function PurchaseOrderDetailPanel({
    po,
    onClose,
    onSubmit,
    onApprove,
    onReject,
    onCancel,
    onReceive,
    actionLoading,
    actionError,
    receiveSubmitting,
    receiveError,
}: Props) {
    const { user } = useAuthStore();
    const [showRejectInput, setShowRejectInput] = useState(false);
    const [showCancelInput, setShowCancelInput] = useState(false);
    const [reasonText, setReasonText] = useState("");
    const [showReceive, setShowReceive] = useState(false);

    const canApprove =
        hasApprovePermission(user) &&
        po.ordered_by !== user?.id;

    const fmt = (d?: string | null) =>
        d ? new Date(d).toLocaleDateString("en-GH", { dateStyle: "medium" }) : null;

    return (
        <>
            {/* Panel */}
            <div className="flex flex-col h-full bg-white border-l border-slate-200">

                {/* Header */}
                <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                    <div>
                        <div className="flex items-center gap-2 mb-1">
                            <POStatusBadge status={po.status} size="md" />
                            <span className="text-xs text-slate-400 font-mono">{po.po_number}</span>
                        </div>
                        <h2 className="text-base font-bold text-ink leading-tight">{po.supplier_name}</h2>
                        <p className="text-xs text-slate-400 mt-0.5">{po.branch_name}</p>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Error banner */}
                {actionError && (
                    <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600 flex-shrink-0">
                        <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                        {actionError}
                    </div>
                )}

                {/* Scrollable body */}
                <div className="flex-1 overflow-y-auto">

                    {/* Meta grid */}
                    <div className="px-5 py-4 grid grid-cols-2 gap-4 border-b border-slate-100">
                        <DetailRow icon={Building2} label="Branch" value={po.branch_name} />
                        <DetailRow icon={User2} label="Created by" value={po.ordered_by_name} />
                        <DetailRow icon={CalendarDays} label="Created" value={fmt(po.created_at)} />
                        <DetailRow
                            icon={Truck}
                            label="Expected delivery"
                            value={fmt(po.expected_delivery_date)}
                        />
                        {po.approved_by_name && (
                            <DetailRow icon={CheckCircle2} label="Approved by" value={po.approved_by_name} />
                        )}
                        {po.approved_at && (
                            <DetailRow icon={Clock} label="Approved at" value={fmt(po.approved_at)} />
                        )}
                    </div>

                    {/* Financials */}
                    <div className="px-5 py-4 space-y-1.5 border-b border-slate-100">
                        <div className="flex justify-between text-xs text-slate-500">
                            <span>Subtotal</span>
                            <span className="font-medium">{fmtGHS(po.subtotal)}</span>
                        </div>
                        {po.shipping_cost > 0 && (
                            <div className="flex justify-between text-xs text-slate-500">
                                <span>Shipping</span>
                                <span className="font-medium">{fmtGHS(po.shipping_cost)}</span>
                            </div>
                        )}
                        <div className="flex justify-between text-sm font-bold text-ink border-t border-slate-100 pt-2">
                            <span>Total</span>
                            <span>{fmtGHS(po.total_amount)}</span>
                        </div>
                    </div>

                    {/* Items */}
                    <div className="border-b border-slate-100">
                        <div className="px-5 py-2.5 flex items-center justify-between">
                            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                                Line Items
                            </span>
                            <span className="text-xs text-slate-400">{po.receipt_progress}</span>
                        </div>
                        <div className="divide-y divide-slate-100">
                            {po.items.map((item) => (
                                <ItemRow key={item.id} item={item} />
                            ))}
                        </div>
                    </div>

                    {/* Notes */}
                    {po.notes && (
                        <div className="px-5 py-4 border-b border-slate-100">
                            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-1">Notes</p>
                            <p className="text-sm text-ink-muted whitespace-pre-wrap">{po.notes}</p>
                        </div>
                    )}
                </div>

                {/* ── Action footer ─────────────────────────────── */}
                <div className="flex-shrink-0 border-t border-slate-200 px-4 py-4 space-y-2">

                    {/* Reject / Cancel reason input */}
                    {(showRejectInput || showCancelInput) && (
                        <div className="space-y-2">
                            <textarea
                                value={reasonText}
                                onChange={(e) => setReasonText(e.target.value)}
                                placeholder={showRejectInput ? "Rejection reason…" : "Cancellation reason…"}
                                rows={2}
                                className="w-full px-3 py-2 text-sm rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 resize-none"
                            />
                            <div className="flex gap-2">
                                <button
                                    onClick={() => {
                                        setShowRejectInput(false);
                                        setShowCancelInput(false);
                                        setReasonText("");
                                    }}
                                    className="flex-1 py-2 text-xs font-semibold border border-slate-200 rounded-xl hover:bg-slate-50 text-ink-secondary transition-colors"
                                >
                                    Back
                                </button>
                                <button
                                    onClick={async () => {
                                        if (!reasonText.trim()) return;
                                        if (showRejectInput) {
                                            await onReject(po.id, reasonText.trim());
                                        } else {
                                            await onCancel(po.id, reasonText.trim());
                                        }
                                        setShowRejectInput(false);
                                        setShowCancelInput(false);
                                        setReasonText("");
                                    }}
                                    disabled={!reasonText.trim() || actionLoading}
                                    className="flex-1 py-2 text-xs font-bold text-white bg-red-500 hover:bg-red-600 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                >
                                    {actionLoading
                                        ? "…"
                                        : showRejectInput
                                            ? "Confirm Rejection"
                                            : "Confirm Cancel"}
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Primary actions — vary by status */}
                    {!showRejectInput && !showCancelInput && (
                        <div className="space-y-2">

                            {/* DRAFT → submit */}
                            {po.status === "draft" && (
                                <>
                                    <button
                                        onClick={() => onSubmit(po.id)}
                                        disabled={actionLoading}
                                        className="w-full py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
                                    >
                                        <Send className="w-4 h-4" />
                                        Submit for Approval
                                    </button>
                                    <button
                                        onClick={() => setShowCancelInput(true)}
                                        className="w-full py-2 text-xs font-semibold text-red-500 hover:text-red-600 hover:bg-red-50 rounded-xl transition-colors"
                                    >
                                        Cancel Order
                                    </button>
                                </>
                            )}

                            {/* PENDING → approve / reject / cancel */}
                            {po.status === "pending" && (
                                <>
                                    {canApprove && (
                                        <button
                                            onClick={() => onApprove(po.id)}
                                            disabled={actionLoading}
                                            className="w-full py-2.5 text-sm font-bold text-white bg-green-600 hover:bg-green-700 rounded-xl disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
                                        >
                                            <CheckCircle2 className="w-4 h-4" />
                                            Approve
                                        </button>
                                    )}
                                    {!canApprove && hasApprovePermission(user) && po.ordered_by === user?.id && (
                                        <div className="flex items-center gap-2 px-3 py-2.5 text-xs text-amber-700 bg-amber-50 rounded-xl border border-amber-200">
                                            <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                                            You cannot approve your own order (four-eyes policy)
                                        </div>
                                    )}
                                    {canApprove && (
                                        <button
                                            onClick={() => setShowRejectInput(true)}
                                            className="w-full py-2.5 text-sm font-bold text-white bg-red-500 hover:bg-red-600 rounded-xl transition-colors flex items-center justify-center gap-2"
                                        >
                                            <XCircle className="w-4 h-4" />
                                            Reject
                                        </button>
                                    )}
                                    <button
                                        onClick={() => setShowCancelInput(true)}
                                        className="w-full py-2 text-xs font-semibold text-red-400 hover:text-red-500 hover:bg-red-50 rounded-xl transition-colors"
                                    >
                                        Cancel Order
                                    </button>
                                </>
                            )}

                            {/* APPROVED | ORDERED → receive goods */}
                            {(po.status === "approved" || po.status === "ordered") && (
                                <button
                                    onClick={() => setShowReceive(true)}
                                    disabled={po.is_fully_received}
                                    className="w-full py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
                                >
                                    <Package2 className="w-4 h-4" />
                                    {po.is_fully_received ? "Fully Received" : "Receive Goods"}
                                </button>
                            )}

                            {/* RECEIVED — read-only */}
                            {po.status === "received" && (
                                <div className="flex items-center justify-center gap-2 py-3 text-sm text-green-700 bg-green-50 rounded-xl border border-green-100">
                                    <CheckCircle2 className="w-4 h-4" />
                                    All goods received on {fmt(po.received_date)}
                                </div>
                            )}

                            {/* CANCELLED — read-only */}
                            {po.status === "cancelled" && (
                                <div className="flex items-center justify-center gap-2 py-3 text-sm text-red-600 bg-red-50 rounded-xl border border-red-100">
                                    <XCircle className="w-4 h-4" />
                                    Order cancelled
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Receive Goods modal */}
            {showReceive && (
                <ReceiveGoodsModal
                    po={po}
                    onSubmit={async (data) => {
                        const ok = await onReceive(data);
                        if (ok) setShowReceive(false);
                        return ok;
                    }}
                    onClose={() => setShowReceive(false)}
                    submitting={receiveSubmitting}
                    submitError={receiveError}
                />
            )}
        </>
    );
}