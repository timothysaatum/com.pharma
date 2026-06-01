/**
 * usePurchaseOrders.ts
 * ─────────────────────────────────────────────────────────────
 * Encapsulates all async state for the Purchase Orders list page:
 *  - Paginated PO list with filters
 *  - Supplier lookup for the create-PO form
 *  - Workflow actions (submit / approve / reject / cancel)
 *  - Optimistic status updates so the table reflects changes instantly
 *
 * Usage:
 *   const po = usePurchaseOrders({ branch_id: activeBranchId });
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { purchaseOrdersApi, suppliersApi, type ListPurchaseOrdersParams } from "@/api/purchases";
import { parseApiError, isOfflineError } from "@/api/client";
import { localRead } from "@/lib/localRead";
import { writeLocal } from "@/lib/localWrite";
import { useAuthStore } from "@/stores/authStore";
import type {
    PurchaseOrder,
    PurchaseOrderCreate,
    PurchaseOrderReject,
    PurchaseOrderCancel,
    PurchaseOrderStatus,
    Supplier,
} from "@/types";

interface UsePurchaseOrdersOptions extends ListPurchaseOrdersParams {
    pageSize?: number;
}

interface ActionState {
    loading: boolean;
    error: string | null;
}

export function usePurchaseOrders(options: UsePurchaseOrdersOptions = {}) {
    const { branch_id, pageSize = 20 } = options;
    const { user } = useAuthStore();

    // ── List state ────────────────────────────────────────────
    const [orders, setOrders] = useState<PurchaseOrder[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [statusFilter, setStatusFilter] = useState<PurchaseOrderStatus | "">("");
    const [supplierFilter, setSupplierFilter] = useState<string>("");
    const [listLoading, setListLoading] = useState(false);
    const [listError, setListError] = useState<string | null>(null);

    // ── Suppliers (for filter + create form) ─────────────────
    const [suppliers, setSuppliers] = useState<Supplier[]>([]);
    const [suppliersLoading, setSuppliersLoading] = useState(false);
    const [suppliersError, setSuppliersError] = useState<string | null>(null);

    // ── Action state ──────────────────────────────────────────
    const [actionState, setActionState] = useState<ActionState>({ loading: false, error: null });

    // ── Create form ───────────────────────────────────────────
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);

    // Abort previous fetch when deps change
    const abortRef = useRef<AbortController | null>(null);

    // ── Fetch paginated list ──────────────────────────────────
    const fetchOrders = useCallback(async (targetPage = 1) => {
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;

        setListLoading(true);
        setListError(null);

        try {
            const params: ListPurchaseOrdersParams = {
                page: targetPage,
                page_size: pageSize,
            };
            if (branch_id) params.branch_id = branch_id;
            if (statusFilter) params.status = statusFilter as PurchaseOrderStatus;
            if (supplierFilter) params.supplier_id = supplierFilter;

            const data = await purchaseOrdersApi.list(params, controller.signal);
            await writeLocal.cachePurchaseOrders(data.items);
            setOrders(data.items);
            setTotal(data.total);
            setTotalPages(data.total_pages);
            setPage(targetPage);
        } catch (err: unknown) {
            const name = (err as { name?: string }).name;
            if (name === "AbortError" || name === "CanceledError") {
                return;
            }
            if (isOfflineError(err)) {
                const data = await localRead.searchPurchaseOrders(
                    {
                        branch_id: branch_id ?? undefined,
                        status: statusFilter || undefined,
                        supplier_id: supplierFilter || undefined,
                    },
                    targetPage,
                    pageSize,
                );
                setOrders(data.items);
                setTotal(data.total);
                setTotalPages(data.total_pages);
                setPage(targetPage);
            } else {
                setListError(parseApiError(err));
            }
        } finally {
            setListLoading(false);
        }
    }, [branch_id, statusFilter, supplierFilter, pageSize]);

    // Re-fetch when filters or branch change
    useEffect(() => {
        fetchOrders(1);
    }, [fetchOrders]);

    // ── Fetch supplier list ───────────────────────────────────
    const fetchSuppliers = useCallback(async () => {
        setSuppliersLoading(true);
        setSuppliersError(null);
        try {
            const data = await suppliersApi.list({ active_only: true, page_size: 200 });
            setSuppliers(data.items);
        } catch (err) {
            if (isOfflineError(err)) {
                const cached = await localRead.getCachedSuppliers(branch_id);
                setSuppliers(cached);
            } else {
                setSuppliersError(parseApiError(err));
            }
        } finally {
            setSuppliersLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchSuppliers();
    }, [fetchSuppliers]);

    // ── Generic action wrapper ────────────────────────────────
    const runAction = useCallback(
        async <T>(fn: () => Promise<T>): Promise<T | null> => {
            setActionState({ loading: true, error: null });
            try {
                const result = await fn();
                setActionState({ loading: false, error: null });
                return result;
            } catch (err) {
                setActionState({ loading: false, error: parseApiError(err) });
                return null;
            }
        },
        [],
    );

    // ── Optimistic status update helper ───────────────────────
    const optimisticUpdate = useCallback(
        (id: string, newStatus: PurchaseOrderStatus) => {
            setOrders((prev) =>
                prev.map((o) => (o.id === id ? { ...o, status: newStatus } : o)),
            );
        },
        [],
    );

    // ── Workflow actions ──────────────────────────────────────
    const submitOrder = useCallback(
        async (id: string) => {
            optimisticUpdate(id, "pending");
            const result = await runAction(() => purchaseOrdersApi.submit(id));
            if (!result) {
                // Revert on failure
                await fetchOrders(page);
            }
            return result;
        },
        [runAction, optimisticUpdate, fetchOrders, page],
    );

    const approveOrder = useCallback(
        async (id: string) => {
            optimisticUpdate(id, "approved");
            const result = await runAction(() => purchaseOrdersApi.approve(id));
            if (!result) await fetchOrders(page);
            return result;
        },
        [runAction, optimisticUpdate, fetchOrders, page],
    );

    const rejectOrder = useCallback(
        async (id: string, data: PurchaseOrderReject) => {
            optimisticUpdate(id, "cancelled");
            const result = await runAction(() => purchaseOrdersApi.reject(id, data));
            if (!result) await fetchOrders(page);
            return result;
        },
        [runAction, optimisticUpdate, fetchOrders, page],
    );

    const cancelOrder = useCallback(
        async (id: string, data: PurchaseOrderCancel) => {
            optimisticUpdate(id, "cancelled");
            const result = await runAction(() => purchaseOrdersApi.cancel(id, data));
            if (!result) await fetchOrders(page);
            return result;
        },
        [runAction, optimisticUpdate, fetchOrders, page],
    );

    // ── Create PO ─────────────────────────────────────────────
    const createOrder = useCallback(
        async (data: PurchaseOrderCreate): Promise<PurchaseOrder | null> => {
            setCreating(true);
            setCreateError(null);
            try {
                const po = await purchaseOrdersApi.create(data);
                await fetchOrders(1); // refresh list
                return po;
            } catch (err) {
                if (isOfflineError(err)) {
                    const now = new Date().toISOString();
                    const subtotal = data.items.reduce(
                        (sum, item) => sum + item.quantity_ordered * item.unit_cost,
                        0,
                    );
                    const po: PurchaseOrder & { items: unknown[] } = {
                        id: crypto.randomUUID(),
                        organization_id: user?.organization_id ?? "",
                        branch_id: data.branch_id,
                        po_number: `OFFLINE-PO-${Date.now()}`,
                        supplier_id: data.supplier_id,
                        subtotal,
                        tax_amount: 0,
                        shipping_cost: data.shipping_cost ?? 0,
                        total_amount: subtotal + (data.shipping_cost ?? 0),
                        status: "draft",
                        ordered_by: user?.id ?? "offline",
                        approved_by: null,
                        approved_at: null,
                        expected_delivery_date: data.expected_delivery_date ?? null,
                        received_date: null,
                        notes: data.notes ?? null,
                        sync_status: "pending",
                        sync_version: 1,
                        synced_at: null,
                        created_at: now,
                        updated_at: now,
                        items: data.items.map((item) => ({
                            id: crypto.randomUUID(),
                            purchase_order_id: "",
                            drug_id: item.drug_id,
                            quantity_ordered: item.quantity_ordered,
                            quantity_received: 0,
                            unit_cost: item.unit_cost,
                            total_cost: item.quantity_ordered * item.unit_cost,
                            batch_number: null,
                            expiry_date: null,
                            is_fully_received: false,
                            remaining_quantity: item.quantity_ordered,
                            created_at: now,
                            updated_at: now,
                        })),
                    };
                    po.items = po.items.map((item) => ({
                        ...(item as Record<string, unknown>),
                        purchase_order_id: po.id,
                    }));
                    await writeLocal.purchaseOrder(po);
                    setOrders((prev) => [po, ...prev]);
                    setTotal((prev) => prev + 1);
                    return po;
                }
                setCreateError(parseApiError(err));
                return null;
            } finally {
                setCreating(false);
            }
        },
        [fetchOrders, user],
    );

    // ── Append a newly created supplier to local list ────────
    // Avoids a full refetch after inline supplier creation.
    const appendSupplier = useCallback((supplier: Supplier) => {
        setSuppliers((prev) => {
            // Insert in alphabetical order (mirrors server sort: name ASC)
            const next = [...(prev ?? []), supplier].sort((a, b) =>
                a.name.localeCompare(b.name),
            );
            return next;
        });
    }, []);

    return {
        // List
        orders,
        total,
        page,
        totalPages,
        listLoading,
        listError,
        // Filters
        statusFilter,
        setStatusFilter,
        supplierFilter,
        setSupplierFilter,
        // Suppliers
        suppliers,
        suppliersLoading,
        suppliersError,
        appendSupplier,
        refreshSuppliers: fetchSuppliers,
        // Actions
        actionState,
        submitOrder,
        approveOrder,
        rejectOrder,
        cancelOrder,
        // Create
        creating,
        createError,
        createOrder,
        // Pagination
        goToPage: fetchOrders,
        refresh: () => fetchOrders(page),
    };
}
