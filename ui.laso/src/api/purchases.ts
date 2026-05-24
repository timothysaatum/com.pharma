/**
 * purchases.ts
 * ─────────────────────────────────────────────────────────────
 * API client for Purchase Orders and Suppliers.
 *
 * Conventions (mirrors the rest of the codebase):
 *  - Uses the typed get/post/patch/del helpers from client.ts which
 *    automatically unwrap `response.data` — do NOT call apiClient directly
 *    as that returns a raw AxiosResponse, not the data.
 *  - Errors surface as `ApiError` — callers use `parseApiError()` to extract
 *    a human-readable string
 *  - Paginated list endpoints accept a `params` object that maps 1-to-1 with
 *    the FastAPI query parameters
 */

import { get, post, patch } from "@/api/client";
import type { PaginatedResponse } from "@/types";
import type {
    PurchaseOrder,
    PurchaseOrderWithDetails,
    PurchaseOrderCreate,
    PurchaseOrderApprove,
    PurchaseOrderReject,
    PurchaseOrderCancel,
    PurchaseOrderItemCreate,
    PurchaseOrderItemWithDetails,
    PurchaseOrderStatus,
    ReceivePurchaseOrder,
    ReceivePurchaseOrderResponse,
    Supplier,
    SupplierCreate,
    SupplierUpdate,
} from "@/types";

// ─── Supplier API ────────────────────────────────────────────────────────────

export interface ListSuppliersParams {
    page?: number;
    page_size?: number;
    active_only?: boolean;
    search?: string;
}

export const suppliersApi = {
    /** Create a new supplier */
    create: (data: SupplierCreate): Promise<Supplier> =>
        post<Supplier>("/suppliers/", data),

    /** Fetch a single supplier */
    get: (id: string): Promise<Supplier> =>
        get<Supplier>(`/suppliers/${id}`),

    /** Partial update of a supplier */
    update: (id: string, data: SupplierUpdate): Promise<Supplier> =>
        patch<Supplier>(`/suppliers/${id}`, data),

    /** Paginated list with optional search / active filter */
    list: (params: ListSuppliersParams = {}): Promise<PaginatedResponse<Supplier>> =>
        get<PaginatedResponse<Supplier>>("/suppliers/", { params: params as Record<string, unknown> }),
};

// ─── Purchase Order API ──────────────────────────────────────────────────────

export interface ListPurchaseOrdersParams {
    page?: number;
    page_size?: number;
    branch_id?: string;
    status?: PurchaseOrderStatus;
    supplier_id?: string;
}

export const purchaseOrdersApi = {
    // ── CRUD ──────────────────────────────────────────────────────────────────

    /** Create a new PO in draft status */
    create: (data: PurchaseOrderCreate): Promise<PurchaseOrder> =>
        post<PurchaseOrder>("/purchase-orders/", data),

    /** Full PO with supplier name, branch name, item details */
    get: (id: string): Promise<PurchaseOrderWithDetails> =>
        get<PurchaseOrderWithDetails>(`/purchase-orders/${id}`),

    /** Paginated list (lightweight — no item detail) */
    list: (params: ListPurchaseOrdersParams = {}, signal?: AbortSignal): Promise<PaginatedResponse<PurchaseOrder>> =>
        get<PaginatedResponse<PurchaseOrder>>("/purchase-orders/", { params: params as Record<string, unknown>, signal }),

    // ── Workflow transitions ──────────────────────────────────────────────────

    /** draft → pending */
    submit: (id: string): Promise<PurchaseOrder> =>
        post<PurchaseOrder>(`/purchase-orders/${id}/submit`),

    /** pending → approved */
    approve: (id: string, data: PurchaseOrderApprove = {}): Promise<PurchaseOrder> =>
        post<PurchaseOrder>(`/purchase-orders/${id}/approve`, data),

    /** pending → cancelled (rejected) */
    reject: (id: string, data: PurchaseOrderReject): Promise<PurchaseOrder> =>
        post<PurchaseOrder>(`/purchase-orders/${id}/reject`, data),

    /** draft | pending → cancelled */
    cancel: (id: string, data: PurchaseOrderCancel): Promise<PurchaseOrder> =>
        post<PurchaseOrder>(`/purchase-orders/${id}/cancel`, data),

    /** approved | ordered → ordered | received */
    receiveGoods: (
        id: string,
        data: ReceivePurchaseOrder,
    ): Promise<ReceivePurchaseOrderResponse> =>
        post<ReceivePurchaseOrderResponse>(`/purchase-orders/${id}/receive`, data),

    // ── Item management ───────────────────────────────────────────────────────

    /** Add items to a draft PO */
    addItems: (
        id: string,
        items: PurchaseOrderItemCreate[],
    ): Promise<PurchaseOrderWithDetails> =>
        post<PurchaseOrderWithDetails>(`/purchase-orders/${id}/items`, items),

    /** Update quantity / unit cost of a single draft PO item */
    updateItem: (
        poId: string,
        itemId: string,
        quantity_ordered: number,
        unit_cost: number,
    ): Promise<PurchaseOrderWithDetails> => {
        const qs = new URLSearchParams({
            quantity_ordered: String(quantity_ordered),
            unit_cost: String(unit_cost),
        }).toString();
        return patch<PurchaseOrderWithDetails>(
            `/purchase-orders/${poId}/items/${itemId}?${qs}`,
        );
    },

    /** All items with drug details */
    listItems: (id: string): Promise<PurchaseOrderItemWithDetails[]> =>
        get<PurchaseOrderItemWithDetails[]>(`/purchase-orders/${id}/items`),
};