/**
 * ─────────────────────────────────────────────────────────────
 * Main Purchase Orders management screen.
 *
 * Layout:
 *  Left (flex-1):  Filterable, paginated PO list
 *  Right (380px):  PO detail slide-over (when a PO is selected)
 *
 * Wires together:
 *  - usePurchaseOrders (list, filters, workflow actions)
 *  - usePurchaseOrderDetail (single PO + receive goods)
 *  - CreatePOModal
 *  - PurchaseOrderDetailPanel
 *
 * Security:
 *  - Page is behind RequireAuth with manage_inventory permission
 *  - Approve / reject buttons only render for users with
 *    approve_purchase_orders permission (enforced server-side too)
 */

import { useState, useCallback } from "react";
import {
    Plus, RefreshCw, AlertTriangle,
    ShoppingBag, ChevronLeft, ChevronRight,
    Building2, SlidersHorizontal,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { usePurchaseOrders } from "@/hooks/usePurchaseOrders";
import { suppliersApi } from "@/api/purchases";
import { usePurchaseOrderDetail } from "@/hooks/usePurchaseOrderDetail";
import { POStatusBadge } from "@/components/purchases/POStatusBadge";
import { CreatePOModal } from "@/components/purchases/CreatePOModal";
import { PurchaseOrderDetailPanel } from "@/components/purchases/PurchaseOrderDetailPanel";
import type { PurchaseOrder, PurchaseOrderStatus, Supplier } from "@/types";

const STATUS_OPTIONS: Array<{ value: PurchaseOrderStatus | ""; label: string }> = [
    { value: "", label: "All statuses" },
    { value: "draft", label: "Draft" },
    { value: "pending", label: "Pending approval" },
    { value: "approved", label: "Approved" },
    { value: "ordered", label: "Partially received" },
    { value: "received", label: "Received" },
    { value: "cancelled", label: "Cancelled" },
];

export default function PurchasesPage() {
    const { user, activeBranchId } = useAuthStore();

    // ── List + actions ────────────────────────────────────────
    const po = usePurchaseOrders({ branch_id: activeBranchId ?? undefined });

    // Guard both against undefined — the hook initialises these as [] but
    // can be undefined if the hook shape diverges (stale build, HMR, etc).
    const orders: PurchaseOrder[] = po.orders ?? [];
    const suppliers: Supplier[] = po.suppliers ?? [];
    const suppliersError = po.suppliersError ?? null;

    // ── Selected PO (detail panel) ────────────────────────────
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const detail = usePurchaseOrderDetail(selectedId);

    // ── Create modal ──────────────────────────────────────────
    const [showCreate, setShowCreate] = useState(false);

    // ── Create supplier state ─────────────────────────────────
    const [creatingSupplier, setCreatingSupplier] = useState(false);
    const [createSupplierError, setCreateSupplierError] = useState<string | null>(null);

    // ── Handle create submit ──────────────────────────────────
    const handleCreate = useCallback(
        async (data: import("@/types").PurchaseOrderCreate): Promise<boolean> => {
            const result = await po.createOrder(data);
            return !!result;
        },
        [po],
    );

    // ── Workflow actions — refresh detail panel on success ────
    const handleSubmit = useCallback(
        async (id: string) => {
            const result = await po.submitOrder(id);
            if (result) detail.refresh();
            return result;
        },
        [po, detail],
    );

    const handleApprove = useCallback(
        async (id: string) => {
            const result = await po.approveOrder(id);
            if (result) detail.refresh();
            return result;
        },
        [po, detail],
    );

    const handleReject = useCallback(
        async (id: string, reason: string) => {
            const result = await po.rejectOrder(id, { reason });
            if (result) detail.refresh();
            return result;
        },
        [po, detail],
    );

    const handleCancel = useCallback(
        async (id: string, reason: string) => {
            const result = await po.cancelOrder(id, { reason });
            if (result) detail.refresh();
            return result;
        },
        [po, detail],
    );

    // ── Create supplier ──────────────────────────────────────
    const handleCreateSupplier = useCallback(
        async (data: import("@/types").SupplierCreate) => {
            setCreatingSupplier(true);
            setCreateSupplierError(null);
            try {
                const supplier = await suppliersApi.create(data);
                // appendSupplier is called via onCreated in CreateSupplierModal
                // so we don't call it here — avoids double-append
                return supplier;
            } catch (err: unknown) {
                const status = (err as { status?: number; response?: { status?: number } })?.status
                    ?? (err as { status?: number; response?: { status?: number } })?.response?.status;
                const msg =
                    status === 409
                        ? "A supplier with this name already exists. Please use a different name."
                        : err instanceof Error
                            ? err.message
                            : "Failed to create supplier";
                setCreateSupplierError(msg);
                return null;
            } finally {
                setCreatingSupplier(false);
            }
        },
        [],
    );

    // ── Select a PO ───────────────────────────────────────────
    const selectPO = (order: PurchaseOrder) => {
        setSelectedId(order.id);
    };

    // ── Receive goods (wires detail hook → detail panel) ──────
    const handleReceive = useCallback(
        async (data: import("@/types").ReceivePurchaseOrder): Promise<boolean> => {
            const result = await detail.receiveGoods(data);
            if (result) {
                po.refresh(); // update the list row status
                return true;
            }
            return false;
        },
        [detail, po],
    );

    return (
        <div className="flex flex-col h-full bg-surface">

            {/* ── Page header ─────────────────────────────────── */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between flex-shrink-0">
                <div>
                    <h1 className="font-display text-2xl font-bold text-ink">Purchases</h1>
                    <p className="text-sm text-ink-muted mt-0.5">
                        {user?.full_name} · {user?.role}
                    </p>
                </div>
                <button
                    onClick={() => setShowCreate(true)}
                    className="flex items-center gap-2 px-4 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors shadow-sm"
                >
                    <Plus className="w-4 h-4" />
                    New Order
                </button>
            </div>

            {/* ── Main body ────────────────────────────────────── */}
            <div className="flex flex-1 min-h-0 overflow-hidden">

                {/* Left: List ────────────────────────────────── */}
                <div className="flex flex-col flex-1 min-h-0 overflow-hidden">

                    {/* Toolbar */}
                    <div className="flex items-center gap-3 px-5 py-3 border-b border-slate-100 bg-white flex-shrink-0">
                        {/* Status filter */}
                        <div className="relative">
                            <SlidersHorizontal className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                            <select
                                value={po.statusFilter}
                                onChange={(e) =>
                                    po.setStatusFilter(e.target.value as PurchaseOrderStatus | "")
                                }
                                className="h-9 pl-9 pr-8 text-sm border border-slate-200 rounded-xl bg-white appearance-none focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                            >
                                {STATUS_OPTIONS.map((o) => (
                                    <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                            </select>
                        </div>

                        {/* Supplier filter */}
                        <div className="relative">
                            <Building2 className="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                            <select
                                value={po.supplierFilter}
                                onChange={(e) => po.setSupplierFilter(e.target.value)}
                                className="h-9 pl-9 pr-8 text-sm border border-slate-200 rounded-xl bg-white appearance-none focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors"
                            >
                                <option value="">All suppliers</option>
                                {/* FIX: use the guarded `suppliers` local variable, not po.suppliers directly */}
                                {suppliers.map((s) => (
                                    <option key={s.id} value={s.id}>{s.name}</option>
                                ))}
                            </select>
                        </div>

                        <div className="flex-1" />

                        {/* Summary */}
                        <span className="text-xs text-slate-400">
                            {po.total} order{po.total !== 1 ? "s" : ""}
                        </span>

                        {/* Refresh */}
                        <button
                            onClick={() => po.refresh()}
                            disabled={po.listLoading}
                            className="p-2 rounded-xl text-slate-400 hover:text-ink hover:bg-slate-100 disabled:opacity-50 transition-colors"
                            title="Refresh"
                        >
                            <RefreshCw className={`w-4 h-4 ${po.listLoading ? "animate-spin" : ""}`} />
                        </button>
                    </div>

                    {/* Error */}
                    {po.listError && (
                        <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600 flex-shrink-0">
                            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                            {po.listError}
                        </div>
                    )}

                    {/* Table */}
                    <div className="flex-1 overflow-y-auto">
                        {po.listLoading && orders.length === 0 ? (
                            <div className="flex items-center justify-center h-full text-slate-400">
                                <RefreshCw className="w-5 h-5 animate-spin mr-2" />
                                Loading orders…
                            </div>
                        ) : orders.length === 0 ? (
                            <div className="flex flex-col items-center justify-center h-full gap-3 py-16 text-center">
                                <div className="w-16 h-16 rounded-3xl bg-slate-100 flex items-center justify-center">
                                    <ShoppingBag className="w-8 h-8 text-slate-300" />
                                </div>
                                <div>
                                    <p className="text-sm font-semibold text-slate-500">No purchase orders</p>
                                    <p className="text-xs text-slate-400 mt-1">
                                        Create your first order using the button above
                                    </p>
                                </div>
                            </div>
                        ) : (
                            <table className="w-full">
                                <thead className="sticky top-0 bg-white border-b border-slate-100 z-10">
                                    <tr>
                                        {["PO Number", "Supplier", "Status", "Total", "Expected", "Created"].map((h) => (
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
                                    {orders.map((order) => (
                                        <tr
                                            key={order.id}
                                            onClick={() => selectPO(order)}
                                            className={`cursor-pointer transition-colors hover:bg-slate-50 ${selectedId === order.id
                                                ? "bg-brand-50/60 hover:bg-brand-50"
                                                : ""
                                                }`}
                                        >
                                            <td className="px-4 py-3">
                                                <span className="text-sm font-bold text-ink font-mono">
                                                    {order.po_number}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                {/* FIX: use the guarded `suppliers` local variable */}
                                                <POSupplierCell order={order} suppliers={suppliers} />
                                            </td>
                                            <td className="px-4 py-3">
                                                <POStatusBadge status={order.status} />
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className="text-sm font-semibold text-ink">
                                                    {fmtGHS(order.total_amount)}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className="text-sm text-slate-500">
                                                    {order.expected_delivery_date
                                                        ? fmtDate(order.expected_delivery_date)
                                                        : "—"}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className="text-sm text-slate-400">
                                                    {fmtDate(order.created_at)}
                                                </span>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>

                    {/* Pagination */}
                    {po.totalPages > 1 && (
                        <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-white flex-shrink-0">
                            <span className="text-xs text-slate-400">
                                Page {po.page} of {po.totalPages}
                            </span>
                            <div className="flex gap-1">
                                <button
                                    onClick={() => po.goToPage(po.page - 1)}
                                    disabled={po.page <= 1 || po.listLoading}
                                    className="p-1.5 rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >
                                    <ChevronLeft className="w-4 h-4" />
                                </button>
                                <button
                                    onClick={() => po.goToPage(po.page + 1)}
                                    disabled={po.page >= po.totalPages || po.listLoading}
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
                    <div className="w-[380px] flex-shrink-0 flex flex-col min-h-0 overflow-hidden border-l border-slate-200">
                        {detail.loading ? (
                            <div className="flex items-center justify-center h-full text-slate-400">
                                <RefreshCw className="w-5 h-5 animate-spin mr-2" />
                                Loading…
                            </div>
                        ) : detail.error ? (
                            <div className="p-5 text-sm text-red-600">
                                <AlertTriangle className="w-4 h-4 inline mr-1" />
                                {detail.error}
                            </div>
                        ) : detail.po ? (
                            <PurchaseOrderDetailPanel
                                po={detail.po}
                                onClose={() => setSelectedId(null)}
                                onSubmit={handleSubmit}
                                onApprove={handleApprove}
                                onReject={(id, reason) => handleReject(id, reason)}
                                onCancel={(id, reason) => handleCancel(id, reason)}
                                onReceive={handleReceive}
                                actionLoading={po.actionState.loading}
                                actionError={po.actionState.error}
                                receiveSubmitting={detail.mutating}
                                receiveError={detail.mutateError}
                            />
                        ) : null}
                    </div>
                )}
            </div>

            {/* Create PO modal */}
            {showCreate && activeBranchId && (
                <CreatePOModal
                    branchId={activeBranchId}
                    suppliers={suppliers}
                    onSubmit={handleCreate}
                    onClose={() => setShowCreate(false)}
                    submitting={po.creating}
                    submitError={po.createError}
                    suppliersError={suppliersError}
                    onRetrySuppliers={po.refreshSuppliers}
                    onCreateSupplier={handleCreateSupplier}
                    onSupplierCreated={po.appendSupplier}
                    createSupplierSubmitting={creatingSupplier}
                    createSupplierError={createSupplierError}
                />
            )}
        </div>
    );
}

// ─── Helper sub-components ───────────────────────────────────────────────────

function POSupplierCell({
    order,
    suppliers,
}: {
    order: PurchaseOrder;
    suppliers: Supplier[];
}) {
    const name =
        suppliers.find((s) => s.id === order.supplier_id)?.name ?? order.supplier_id.slice(0, 8);
    return <span className="text-sm text-ink-secondary">{name}</span>;
}

function fmtDate(iso: string): string {
    return new Date(iso).toLocaleDateString("en-GH", { dateStyle: "medium" });
}

// Python Decimal serialises to a JSON string — always coerce with Number()
// before calling .toFixed() so the page never crashes on string values.
function fmtGHS(value: number | string | null | undefined): string {
    return `₵${Number(value ?? 0).toFixed(2)}`;
}