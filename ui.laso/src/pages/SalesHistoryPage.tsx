/**
 * ─────────────────────────────────────────────────────────────
 * Paginated, filterable sales history for the current branch.
 *
 * Layout:
 *   Left (flex-1):  Filterable, paginated sales table
 *   Right (420px):  Sale detail slide-over (when a sale is selected)
 *
 * Features:
 *  - Filter by status, payment method, date range
 *  - Search by sale number or customer name
 *  - Detail panel: full line items, totals, contract, insurance
 *  - Receipt view within the detail panel
 *  - Role-aware: refund button only for users with process_refunds permission
 *
 * Security:
 *  - Page is behind RequireAuth with process_sales permission
 *  - Refund action enforced server-side too
 */

import { useState, useEffect, useCallback, useRef } from "react";
import {
    Search, RefreshCw, AlertTriangle, Receipt,
    ChevronLeft, ChevronRight, X, CheckCircle2,
    XCircle, Clock, RotateCcw, CreditCard, Banknote,
    Smartphone, Shield, ShoppingBag, TrendingUp,
    SlidersHorizontal, Calendar, User,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { salesApi } from "@/api/sales";
import { parseApiError } from "@/api/client";
import type { Sale, SaleWithDetails } from "@/types";
import type { ReceiptData } from "@/api/sales";
import { useDebounce } from "@/hooks/useDebounce";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 25;

const STATUS_OPTIONS = [
    { value: "", label: "All statuses" },
    { value: "completed", label: "Completed" },
    { value: "draft", label: "Draft" },
    { value: "cancelled", label: "Cancelled" },
    { value: "refunded", label: "Refunded" },
];

const PAYMENT_OPTIONS = [
    { value: "", label: "All methods" },
    { value: "cash", label: "Cash" },
    { value: "card", label: "Card" },
    { value: "mobile_money", label: "Mobile Money" },
    { value: "insurance", label: "Insurance" },
    { value: "credit", label: "Credit" },
    { value: "split", label: "Split" },
];

const STATUS_CONFIG: Record<string, { label: string; icon: React.ElementType; cls: string }> = {
    completed: { label: "Completed", icon: CheckCircle2, cls: "bg-green-50 text-green-700 border-green-100" },
    draft: { label: "Draft", icon: Clock, cls: "bg-slate-50 text-slate-600 border-slate-200" },
    cancelled: { label: "Cancelled", icon: XCircle, cls: "bg-red-50 text-red-600 border-red-100" },
    refunded: { label: "Refunded", icon: RotateCcw, cls: "bg-amber-50 text-amber-700 border-amber-100" },
};

const PAYMENT_ICON: Record<string, React.ElementType> = {
    cash: Banknote,
    card: CreditCard,
    mobile_money: Smartphone,
    insurance: Shield,
    credit: Receipt,
    split: TrendingUp,
};

const PAYMENT_LABEL: Record<string, string> = {
    cash: "Cash", card: "Card", mobile_money: "Mobile Money",
    insurance: "Insurance", credit: "Credit", split: "Split",
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtDate(iso: string) {
    return new Date(iso).toLocaleDateString("en-GH", { dateStyle: "medium" });
}

function fmtDateTime(iso: string) {
    return new Date(iso).toLocaleString("en-GH", {
        dateStyle: "medium", timeStyle: "short",
    });
}

function fmtGHS(n: number | string | null | undefined) {
    return `₵${Number(n ?? 0).toFixed(2)}`;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
    const cfg = STATUS_CONFIG[status] ?? { label: status, icon: Clock, cls: "bg-slate-50 text-slate-500 border-slate-200" };
    const Icon = cfg.icon;
    return (
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-bold rounded-full border ${cfg.cls}`}>
            <Icon className="w-3 h-3" />
            {cfg.label}
        </span>
    );
}

function PaymentBadge({ method }: { method: string }) {
    const Icon = PAYMENT_ICON[method] ?? Receipt;
    return (
        <span className="inline-flex items-center gap-1 text-xs text-slate-500">
            <Icon className="w-3.5 h-3.5" />
            {PAYMENT_LABEL[method] ?? method}
        </span>
    );
}

// ─── Detail Panel ─────────────────────────────────────────────────────────────

function SaleDetailPanel({
    saleId,
    onClose,
    canRefund,
}: {
    saleId: string;
    onClose: () => void;
    canRefund: boolean;
}) {
    const [sale, setSale] = useState<SaleWithDetails | null>(null);
    const [receipt, setReceipt] = useState<ReceiptData | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [view, setView] = useState<"detail" | "receipt">("detail");

    // ── FIX: ref to scope the print output to just the receipt content ──
    const receiptRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        setSale(null);
        setReceipt(null);
        setView("detail");

        salesApi.getById(saleId)
            .then((data) => { if (!cancelled) { setSale(data); setLoading(false); } })
            .catch((err) => { if (!cancelled) { setError(parseApiError(err)); setLoading(false); } });

        return () => { cancelled = true; };
    }, [saleId]);

    const loadReceipt = useCallback(async () => {
        if (receipt) { setView("receipt"); return; }
        try {
            const data = await salesApi.getReceipt(saleId);
            setReceipt(data);
            setView("receipt");
        } catch (err) {
            setError(parseApiError(err));
        }
    }, [saleId, receipt]);

    // Print via a hidden iframe — avoids popup blockers and the race condition
    // where win.close() killed the window before the print dialog opened.
    const handlePrint = () => {
        if (!receipt) return;

        // Build print HTML directly from receipt data (not from innerHTML)
        // so we don't depend on Tailwind class resolution in a foreign document.
        const html = `<!DOCTYPE html>
<html>
<head>
  <title>Receipt – ${receipt.receipt_number}</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: monospace; font-size: 12px; padding: 20px; max-width: 380px; color: #1a1a1a; }
    .center { text-align: center; }
    .bold { font-weight: 700; }
    .semibold { font-weight: 600; }
    .row { display: flex; justify-content: space-between; margin-bottom: 3px; }
    .muted { color: #64748b; }
    .small { font-size: 11px; }
    .large { font-size: 15px; }
    .section { border-bottom: 1px dashed #ccc; padding-bottom: 10px; margin-bottom: 10px; }
    .green { color: #16a34a; }
    .emerald { color: #047857; }
    @media print { body { padding: 0; } }
  </style>
</head>
<body>
  <div class="section center">
    <p class="bold large">${receipt.organization.name}</p>
    ${receipt.branch.name ? `<p class="muted">${receipt.branch.name}</p>` : ""}
    ${receipt.branch.phone ? `<p class="muted">${receipt.branch.phone}</p>` : ""}
    <p class="muted small" style="margin-top:6px">${new Date(receipt.receipt_date).toLocaleString("en-GH")}</p>
    <p class="bold">${receipt.receipt_number}</p>
  </div>
  ${receipt.customer.name ? `
  <div class="section small">
    <div class="row"><span class="muted">Customer</span><span class="semibold">${receipt.customer.name}</span></div>
    ${receipt.customer.phone ? `<div class="row"><span class="muted">Phone</span><span>${receipt.customer.phone}</span></div>` : ""}
  </div>` : ""}
  <div class="section">
    ${receipt.items.map(item => `
      <div style="margin-bottom:6px">
        <div class="row"><span class="semibold" style="flex:1;margin-right:8px">${item.name}</span><span>${Number(item.total).toFixed(2)}</span></div>
        <div class="row small muted"><span>${item.quantity} × ${Number(item.unit_price).toFixed(2)}</span>${item.total_discount > 0 ? `<span class="green">−${Number(item.total_discount).toFixed(2)}</span>` : ""}</div>
      </div>`).join("")}
  </div>
  <div class="section">
    <div class="row muted small"><span>Subtotal</span><span>${Number(receipt.subtotal).toFixed(2)}</span></div>
    ${receipt.total_discount > 0 ? `<div class="row green small"><span>Discount</span><span>−${Number(receipt.total_discount).toFixed(2)}</span></div>` : ""}
    ${receipt.tax > 0 ? `<div class="row muted small"><span>Tax</span><span>${Number(receipt.tax).toFixed(2)}</span></div>` : ""}
    <div class="row bold large" style="margin-top:6px;padding-top:6px;border-top:1px solid #ccc"><span>TOTAL</span><span>₵${Number(receipt.total).toFixed(2)}</span></div>
    <div class="row muted small" style="margin-top:4px"><span>Paid (${receipt.payment_method})</span><span>₵${Number(receipt.amount_paid).toFixed(2)}</span></div>
    ${receipt.change > 0 ? `<div class="row emerald semibold"><span>Change</span><span>₵${Number(receipt.change).toFixed(2)}</span></div>` : ""}
  </div>
  ${receipt.contract ? `<div class="section small center muted"><p>${receipt.contract.name} — ${receipt.contract.discount_percentage}% discount</p></div>` : ""}
  <div class="center muted small" style="margin-top:8px">
    ${receipt.cashier ? `<p>Served by: ${receipt.cashier}</p>` : ""}
    <p style="margin-top:6px;font-weight:600;color:#1a1a1a">Thank you for your purchase!</p>
  </div>
</body>
</html>`;

        // Inject a hidden iframe, write the HTML, print, then remove it.
        // This never triggers popup blockers and has no race condition.
        const iframe = document.createElement("iframe");
        iframe.style.cssText = "position:fixed;top:0;left:0;width:0;height:0;border:none;visibility:hidden;";
        document.body.appendChild(iframe);

        const iframeDoc = iframe.contentDocument ?? iframe.contentWindow?.document;
        if (!iframeDoc) { document.body.removeChild(iframe); return; }

        iframeDoc.open();
        iframeDoc.write(html);
        iframeDoc.close();

        // Wait for iframe content to fully render before printing
        iframe.onload = () => {
            iframe.contentWindow?.focus();
            iframe.contentWindow?.print();
            // Remove iframe after a short delay so the print dialog can open
            setTimeout(() => document.body.removeChild(iframe), 1000);
        };
    };

    return (
        <div className="flex flex-col h-full bg-white border-l border-slate-200">
            {/* Panel header */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div>
                    <p className="text-xs font-bold text-ink-muted uppercase tracking-widest mb-0.5">Sale Detail</p>
                    {sale && (
                        <p className="text-sm font-bold text-ink font-mono">{sale.sale_number}</p>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    {sale && (
                        <div className="flex rounded-lg border border-slate-200 overflow-hidden">
                            <button
                                onClick={() => setView("detail")}
                                className={`px-3 py-1.5 text-xs font-semibold transition-colors ${view === "detail" ? "bg-brand-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}
                            >
                                Detail
                            </button>
                            <button
                                onClick={loadReceipt}
                                className={`px-3 py-1.5 text-xs font-semibold transition-colors ${view === "receipt" ? "bg-brand-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}
                            >
                                Receipt
                            </button>
                        </div>
                    )}
                    <button
                        onClick={onClose}
                        className="p-1.5 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {/* Panel body */}
            <div className="flex-1 overflow-y-auto min-h-0">
                {loading && (
                    <div className="flex items-center justify-center h-32 text-slate-400">
                        <RefreshCw className="w-5 h-5 animate-spin mr-2" /> Loading…
                    </div>
                )}
                {error && (
                    <div className="m-4 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600 flex gap-2">
                        <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" /> {error}
                    </div>
                )}

                {sale && view === "detail" && (
                    <div className="p-5 space-y-5">
                        {/* Status + meta */}
                        <div className="flex items-start justify-between">
                            <div className="space-y-1">
                                <StatusBadge status={sale.status} />
                                <p className="text-xs text-slate-400 mt-1">{fmtDateTime(sale.created_at)}</p>
                            </div>
                            <div className="text-right">
                                <p className="text-2xl font-bold text-ink">{fmtGHS(sale.total_amount)}</p>
                                <PaymentBadge method={sale.payment_method} />
                            </div>
                        </div>

                        {/* Customer + cashier */}
                        <div className="grid grid-cols-2 gap-3">
                            <div className="rounded-xl bg-slate-50 p-3">
                                <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-1">Customer</p>
                                <p className="text-sm font-semibold text-ink">
                                    {(sale as SaleWithDetails & { customer_full_name?: string }).customer_full_name
                                        || sale.customer_name
                                        || "Walk-in"}
                                </p>
                                {(sale as SaleWithDetails & { customer_phone?: string }).customer_phone && (
                                    <p className="text-xs text-slate-400 mt-0.5">
                                        {(sale as SaleWithDetails & { customer_phone?: string }).customer_phone}
                                    </p>
                                )}
                            </div>
                            <div className="rounded-xl bg-slate-50 p-3">
                                <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-1">Cashier</p>
                                <p className="text-sm font-semibold text-ink">
                                    {(sale as SaleWithDetails & { cashier_name?: string }).cashier_name ?? "—"}
                                </p>
                            </div>
                        </div>

                        {/* Contract / insurance */}
                        {(sale as SaleWithDetails & { contract_name?: string }).contract_name && (
                            <div className="rounded-xl bg-brand-50 border border-brand-100 p-3 flex items-start gap-2">
                                <TrendingUp className="w-4 h-4 text-brand-600 flex-shrink-0 mt-0.5" />
                                <div>
                                    <p className="text-xs font-semibold text-brand-700">
                                        {(sale as SaleWithDetails & { contract_name?: string }).contract_name}
                                    </p>
                                    {Number((sale as SaleWithDetails & { contract_discount_percentage?: number }).contract_discount_percentage) > 0 && (
                                        <p className="text-xs text-brand-600">
                                            {(sale as SaleWithDetails & { contract_discount_percentage?: number }).contract_discount_percentage}% contract discount
                                        </p>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* Line items */}
                        <div>
                            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2">
                                Items ({sale.items?.length ?? 0})
                            </p>
                            <div className="rounded-xl border border-slate-100 overflow-hidden divide-y divide-slate-100">
                                {sale.items?.map((item) => (
                                    <div key={item.id} className="px-4 py-3 flex items-center justify-between">
                                        <div className="min-w-0 flex-1">
                                            <p className="text-sm font-semibold text-ink truncate">{item.drug_name}</p>
                                            <p className="text-xs text-slate-400 mt-0.5">
                                                {item.quantity} × {fmtGHS(item.unit_price)}
                                                {item.batch_number && (
                                                    <span className="ml-2 font-mono bg-slate-100 px-1 rounded text-[10px]">
                                                        {item.batch_number}
                                                    </span>
                                                )}
                                            </p>
                                        </div>
                                        <div className="text-right ml-3 flex-shrink-0">
                                            <p className="text-sm font-bold text-ink">{fmtGHS(item.total_price)}</p>
                                            {Number(item.total_discount_amount) > 0 && (
                                                <p className="text-xs text-green-600">
                                                    −{fmtGHS(item.total_discount_amount)}
                                                </p>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>

                        {/* Totals breakdown */}
                        <div className="rounded-xl bg-slate-50 p-4 space-y-2">
                            <div className="flex justify-between text-xs text-slate-500">
                                <span>Subtotal</span>
                                <span>{fmtGHS(sale.subtotal)}</span>
                            </div>
                            {Number(sale.total_discount_amount) > 0 && (
                                <div className="flex justify-between text-xs text-green-600">
                                    <span>Total discount</span>
                                    <span>−{fmtGHS(sale.total_discount_amount)}</span>
                                </div>
                            )}
                            {Number(sale.tax_amount) > 0 && (
                                <div className="flex justify-between text-xs text-slate-500">
                                    <span>Tax</span>
                                    <span>{fmtGHS(sale.tax_amount)}</span>
                                </div>
                            )}
                            <div className="flex justify-between text-sm font-bold text-ink border-t border-slate-200 pt-2">
                                <span>Total</span>
                                <span>{fmtGHS(sale.total_amount)}</span>
                            </div>
                            {Number(sale.amount_paid) > 0 && (
                                <div className="flex justify-between text-xs text-slate-500">
                                    <span>Amount paid</span>
                                    <span>{fmtGHS(sale.amount_paid)}</span>
                                </div>
                            )}
                            {Number(sale.change_amount) > 0 && (
                                <div className="flex justify-between text-xs font-semibold text-emerald-700">
                                    <span>Change</span>
                                    <span>{fmtGHS(sale.change_amount)}</span>
                                </div>
                            )}
                        </div>

                        {/* Notes */}
                        {(sale as SaleWithDetails & { notes?: string }).notes && (
                            <div className="rounded-xl bg-amber-50 border border-amber-100 p-3 text-xs text-amber-700">
                                <p className="font-semibold mb-1">Notes</p>
                                <p>{(sale as SaleWithDetails & { notes?: string }).notes}</p>
                            </div>
                        )}

                        {/* Actions */}
                        <div className="flex gap-2 pt-1">
                            <button
                                onClick={loadReceipt}
                                type="button"
                                className="flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-semibold border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors"
                            >
                                <Receipt className="w-4 h-4" />
                                View Receipt
                            </button>
                            {canRefund && sale.status === "completed" && (
                                <button
                                    type="button"
                                    className="flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-semibold text-amber-700 border border-amber-200 bg-amber-50 hover:bg-amber-100 rounded-xl transition-colors"
                                >
                                    <RotateCcw className="w-4 h-4" />
                                    Refund
                                </button>
                            )}
                        </div>
                    </div>
                )}

                {/* Receipt view */}
                {sale && view === "receipt" && receipt && (
                    <div ref={receiptRef} className="p-5 font-mono text-xs space-y-4">
                        {/* Receipt header */}
                        <div className="text-center space-y-1 border-b border-dashed border-slate-300 pb-4">
                            <p className="font-bold text-base font-sans text-ink">{receipt.organization.name}</p>
                            {receipt.branch.name && (
                                <p className="text-slate-500">{receipt.branch.name}</p>
                            )}
                            {receipt.branch.phone && <p className="text-slate-500">{receipt.branch.phone}</p>}
                            <p className="text-slate-400 mt-2">{fmtDateTime(receipt.receipt_date)}</p>
                            <p className="font-bold text-ink">{receipt.receipt_number}</p>
                        </div>

                        {/* Customer */}
                        {receipt.customer.name && (
                            <div className="border-b border-dashed border-slate-200 pb-3">
                                <p className="text-slate-500">Customer: <span className="text-ink font-semibold">{receipt.customer.name}</span></p>
                                {receipt.customer.phone && <p className="text-slate-500">Phone: {receipt.customer.phone}</p>}
                            </div>
                        )}

                        {/* Items */}
                        <div className="space-y-2 border-b border-dashed border-slate-200 pb-3">
                            {receipt.items.map((item, i) => (
                                <div key={i}>
                                    <div className="flex justify-between">
                                        <span className="font-semibold text-ink flex-1 mr-2">{item.name}</span>
                                        <span>{fmtGHS(item.total)}</span>
                                    </div>
                                    <div className="flex justify-between text-slate-400">
                                        <span>{item.quantity} × {fmtGHS(item.unit_price)}</span>
                                        {item.total_discount > 0 && (
                                            <span className="text-green-600">−{fmtGHS(item.total_discount)}</span>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>

                        {/* Totals */}
                        <div className="space-y-1 border-b border-dashed border-slate-200 pb-3">
                            <div className="flex justify-between text-slate-500">
                                <span>Subtotal</span><span>{fmtGHS(receipt.subtotal)}</span>
                            </div>
                            {receipt.total_discount > 0 && (
                                <div className="flex justify-between text-green-600">
                                    <span>Discount</span><span>−{fmtGHS(receipt.total_discount)}</span>
                                </div>
                            )}
                            {receipt.tax > 0 && (
                                <div className="flex justify-between text-slate-500">
                                    <span>Tax</span><span>{fmtGHS(receipt.tax)}</span>
                                </div>
                            )}
                            <div className="flex justify-between font-bold text-ink text-sm">
                                <span>TOTAL</span><span>{fmtGHS(receipt.total)}</span>
                            </div>
                            <div className="flex justify-between text-slate-500">
                                <span>Paid ({PAYMENT_LABEL[receipt.payment_method] ?? receipt.payment_method})</span>
                                <span>{fmtGHS(receipt.amount_paid)}</span>
                            </div>
                            {receipt.change > 0 && (
                                <div className="flex justify-between font-semibold text-emerald-700">
                                    <span>Change</span><span>{fmtGHS(receipt.change)}</span>
                                </div>
                            )}
                        </div>

                        {/* Contract */}
                        {receipt.contract && (
                            <div className="text-center text-slate-400 border-b border-dashed border-slate-200 pb-3">
                                <p>Contract: {receipt.contract.name}</p>
                                <p>Discount: {receipt.contract.discount_percentage}%</p>
                            </div>
                        )}

                        {/* Cashier footer */}
                        <div className="text-center text-slate-400 pt-2">
                            {receipt.cashier && <p>Served by: {receipt.cashier}</p>}
                            <p className="mt-2 font-sans font-semibold">Thank you for your purchase!</p>
                        </div>

                        {/* Print button — hidden inside the printed window via CSS */}
                        <button
                            onClick={handlePrint}
                            type="button"
                            className="w-full py-2.5 text-sm font-semibold font-sans border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors flex items-center justify-center gap-2"
                        >
                            <Receipt className="w-4 h-4" />
                            Print Receipt
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function SalesHistoryPage() {
    const { user, activeBranchId } = useAuthStore();
    // Roles that have process_refunds by default
    const REFUND_ROLES = ["manager", "admin", "super_admin"];
    // user.permissions can be null/undefined when the API omits the field
    // (e.g. during an active session whose token predates the permissions column).
    // Optional chaining + fallback arrays prevent a crash before the first render.
    const canRefund = user
        ? REFUND_ROLES.includes(user.role) ||
        ((user.permissions?.additional ?? []).includes("process_refunds") &&
            !(user.permissions?.denied ?? []).includes("process_refunds"))
        : false;

    // ── List state ────────────────────────────────────────────
    const [sales, setSales] = useState<Sale[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // ── Filters ───────────────────────────────────────────────
    const [search, setSearch] = useState("");
    const [statusFilter, setStatusFilter] = useState("");
    const [paymentFilter, setPaymentFilter] = useState("");
    const [startDate, setStartDate] = useState("");
    const [endDate, setEndDate] = useState("");
    const debouncedSearch = useDebounce(search, 350);

    // ── Selected sale (detail panel) ──────────────────────────
    const [selectedId, setSelectedId] = useState<string | null>(null);

    const abortRef = useRef<AbortController | null>(null);

    // ── Fetch ─────────────────────────────────────────────────
    const fetchSales = useCallback(async (targetPage = 1) => {
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;

        setLoading(true);
        setError(null);

        try {
            const data = await salesApi.list(
                {
                    page: targetPage,
                    page_size: PAGE_SIZE,
                    branch_id: activeBranchId ?? undefined,
                    status: statusFilter || undefined,
                    payment_method: paymentFilter || undefined,
                    start_date: startDate || undefined,
                    end_date: endDate || undefined,
                },
                controller.signal,
            );
            if (!controller.signal.aborted) {
                setSales(data.items);
                setTotal(data.total);
                setTotalPages(data.total_pages);
                setPage(targetPage);
            }
        } catch (err: unknown) {
            if (!controller.signal.aborted) {
                setError(parseApiError(err));
            }
        } finally {
            if (!controller.signal.aborted) setLoading(false);
        }
    }, [activeBranchId, statusFilter, paymentFilter, startDate, endDate]);

    useEffect(() => {
        fetchSales(1);
        return () => abortRef.current?.abort();
    }, [fetchSales]);

    // Client-side search filter (sale number / customer name)
    const filtered = debouncedSearch
        ? sales.filter(
            (s) =>
                s.sale_number.toLowerCase().includes(debouncedSearch.toLowerCase()) ||
                (s.customer_name ?? "").toLowerCase().includes(debouncedSearch.toLowerCase()),
        )
        : sales;

    const hasActiveFilters = statusFilter || paymentFilter || startDate || endDate;

    const clearFilters = () => {
        setStatusFilter("");
        setPaymentFilter("");
        setStartDate("");
        setEndDate("");
        setSearch("");
    };

    return (
        <div className="flex flex-col h-full bg-surface">

            {/* ── Page header ─────────────────────────────────── */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between flex-shrink-0">
                <div>
                    <h1 className="font-display text-2xl font-bold text-ink">Sales History</h1>
                    <p className="text-sm text-ink-muted mt-0.5">
                        {user?.full_name} · {user?.role}
                        {total > 0 && (
                            <span className="ml-2 text-ink-muted">· {total.toLocaleString()} sales</span>
                        )}
                    </p>
                </div>
                <button
                    onClick={() => fetchSales(page)}
                    disabled={loading}
                    className="flex items-center gap-2 px-3 py-2 text-sm text-slate-500 border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
                    Refresh
                </button>
            </div>

            {/* ── Toolbar ──────────────────────────────────────── */}
            <div className="flex items-center gap-3 px-5 py-3 border-b border-slate-100 bg-white flex-shrink-0 flex-wrap">

                {/* Search */}
                <div className="relative flex-1 min-w-[200px] max-w-xs">
                    <Search className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder="Sale # or customer…"
                        className="w-full pl-8 pr-3 h-9 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                    />
                </div>

                {/* Status filter */}
                <div className="relative">
                    <SlidersHorizontal className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                    <select
                        value={statusFilter}
                        onChange={(e) => setStatusFilter(e.target.value)}
                        className="h-9 pl-8 pr-8 text-sm border border-slate-200 rounded-xl bg-white appearance-none focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                    >
                        {STATUS_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                    </select>
                </div>

                {/* Payment filter */}
                <div className="relative">
                    <CreditCard className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                    <select
                        value={paymentFilter}
                        onChange={(e) => setPaymentFilter(e.target.value)}
                        className="h-9 pl-8 pr-8 text-sm border border-slate-200 rounded-xl bg-white appearance-none focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                    >
                        {PAYMENT_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                    </select>
                </div>

                {/* Date range */}
                <div className="flex items-center gap-2">
                    <div className="relative">
                        <Calendar className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                        <input
                            type="date"
                            value={startDate}
                            onChange={(e) => setStartDate(e.target.value)}
                            className="h-9 pl-8 pr-3 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                        />
                    </div>
                    <span className="text-slate-400 text-xs">to</span>
                    <input
                        type="date"
                        value={endDate}
                        onChange={(e) => setEndDate(e.target.value)}
                        min={startDate}
                        className="h-9 px-3 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                    />
                </div>

                {/* Clear filters */}
                {(hasActiveFilters || search) && (
                    <button
                        onClick={clearFilters}
                        className="flex items-center gap-1.5 px-3 h-9 text-xs font-semibold text-slate-500 border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors"
                    >
                        <X className="w-3.5 h-3.5" />
                        Clear
                    </button>
                )}
            </div>

            {/* ── Main body ────────────────────────────────────── */}
            <div className="flex flex-1 min-h-0 overflow-hidden">

                {/* Left: Table ───────────────────────────────── */}
                <div className="flex flex-col flex-1 min-h-0 overflow-hidden">

                    {/* Error banner */}
                    {error && (
                        <div className="mx-5 mt-3 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600 flex gap-2 flex-shrink-0">
                            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                            {error}
                        </div>
                    )}

                    {/* Table */}
                    <div className="flex-1 overflow-y-auto min-h-0">
                        {loading && sales.length === 0 ? (
                            <div className="flex items-center justify-center h-48 text-slate-400">
                                <RefreshCw className="w-5 h-5 animate-spin mr-2" /> Loading sales…
                            </div>
                        ) : filtered.length === 0 ? (
                            <div className="flex flex-col items-center justify-center h-48 gap-3 text-slate-400">
                                <ShoppingBag className="w-10 h-10 opacity-30" />
                                <div className="text-center">
                                    <p className="text-sm font-semibold text-slate-500">No sales found</p>
                                    <p className="text-xs mt-1">
                                        {hasActiveFilters || search ? "Try adjusting your filters" : "Sales will appear here once processed"}
                                    </p>
                                </div>
                            </div>
                        ) : (
                            <table className="w-full">
                                <thead className="sticky top-0 bg-white border-b border-slate-100 z-10">
                                    <tr>
                                        {["Sale #", "Date", "Customer", "Payment", "Total", "Status"].map((h) => (
                                            <th
                                                key={h}
                                                className="px-4 py-2.5 text-left text-[10px] font-bold text-slate-400 uppercase tracking-widest"
                                            >
                                                {h}
                                            </th>
                                        ))}
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-slate-50">
                                    {filtered.map((sale) => (
                                        <tr
                                            key={sale.id}
                                            onClick={() => setSelectedId(sale.id)}
                                            className={`cursor-pointer transition-colors hover:bg-slate-50 ${selectedId === sale.id ? "bg-brand-50/60 hover:bg-brand-50" : ""}`}
                                        >
                                            <td className="px-4 py-3">
                                                <span className="text-sm font-bold text-ink font-mono">
                                                    {sale.sale_number}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className="text-sm text-slate-500">{fmtDate(sale.created_at)}</span>
                                            </td>
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-1.5">
                                                    <User className="w-3.5 h-3.5 text-slate-300 flex-shrink-0" />
                                                    <span className="text-sm text-ink">
                                                        {sale.customer_name || "Walk-in"}
                                                    </span>
                                                </div>
                                            </td>

                                            <td className="px-4 py-3">
                                                <PaymentBadge method={sale.payment_method} />
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className="text-sm font-bold text-ink">
                                                    {fmtGHS(sale.total_amount)}
                                                </span>
                                                {Number(sale.discount_amount) > 0 && (
                                                    <p className="text-[10px] text-green-600">
                                                        −{fmtGHS(sale.discount_amount)} saved
                                                    </p>
                                                )}
                                            </td>
                                            <td className="px-4 py-3">
                                                <StatusBadge status={sale.status} />
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>

                    {/* Pagination */}
                    {totalPages > 1 && (
                        <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-white flex-shrink-0">
                            <span className="text-xs text-slate-400">
                                Page {page} of {totalPages} · {total.toLocaleString()} total
                            </span>
                            <div className="flex gap-1">
                                <button
                                    onClick={() => fetchSales(page - 1)}
                                    disabled={page <= 1 || loading}
                                    className="p-1.5 rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >
                                    <ChevronLeft className="w-4 h-4" />
                                </button>
                                <button
                                    onClick={() => fetchSales(page + 1)}
                                    disabled={page >= totalPages || loading}
                                    className="p-1.5 rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >
                                    <ChevronRight className="w-4 h-4" />
                                </button>
                            </div>
                        </div>
                    )}
                </div>

                {/* Right: Detail panel ────────────────────────── */}
                {selectedId && (
                    <div className="w-[420px] flex-shrink-0 flex flex-col min-h-0 overflow-hidden">
                        <SaleDetailPanel
                            saleId={selectedId}
                            onClose={() => setSelectedId(null)}
                            canRefund={canRefund}
                        />
                    </div>
                )}
            </div>
        </div>
    );
}