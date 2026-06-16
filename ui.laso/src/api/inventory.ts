/**
 * ================
 * HTTP wrappers for the inventory endpoints.
 * All URLs are verified against the FastAPI router in inventory_routes.py.
 */

import { del, get, patch, post } from "./client";
import type {
    BranchInventory,
    BranchInventoryWithDetails,
    DrugBatch,
    DrugBatchCreate,
    DrugBatchUpdate,
    InventoryValuationResponse,
    PaginatedResponse,
    StockAdjustmentCreate,
    StockAdjustmentResponse,
    // These four types were previously declared locally; they now live in
    // @/types so any module can import them without depending on the API layer.
    LowStockReport,
    ExpiringBatchReport,
    StockTransferCreate,
    StockTransferResponse,
} from "@/types";

const BASE = "/inventory";

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────

export const inventoryApi = {
    // ── Branch Inventory ─────────────────────────────────────────────────────

    /**
     * GET /inventory/branch/{branch_id}
     * Paginated inventory list for a branch with joined drug/branch details.
     * Pass an AbortSignal to cancel on component unmount.
     */
    getBranchInventory(
        branchId: string,
        params: {
            page?: number;
            page_size?: number;
            drug_id?: string;
            include_zero_stock?: boolean;
            search?: string;
            low_stock_only?: boolean;
            drug_type?: string;
        } = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<BranchInventoryWithDetails>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<BranchInventoryWithDetails>>(
            `${BASE}/branch/${branchId}?${qs}`,
            { signal }
        );
    },

    /**
     * POST /inventory/reserve
     * Reserve stock for a pending order or prescription.
     * Params are sent as query parameters (not body) per the router.
     */
    reserveInventory(
        branchId: string,
        drugId: string,
        quantity: number
    ): Promise<{ success: boolean; message: string; inventory: BranchInventory }> {
        const qs = new URLSearchParams({
            branch_id: branchId,
            drug_id: drugId,
            quantity: String(quantity),
        });
        return post(`${BASE}/reserve?${qs}`);
    },

    /**
     * POST /inventory/release-reserved
     * Release previously reserved stock (e.g. order cancelled).
     * Params are sent as query parameters (not body) per the router.
     */
    releaseReserved(
        branchId: string,
        drugId: string,
        quantity: number
    ): Promise<{ success: boolean; message: string; inventory: BranchInventory }> {
        const qs = new URLSearchParams({
            branch_id: branchId,
            drug_id: drugId,
            quantity: String(quantity),
        });
        return post(`${BASE}/release-reserved?${qs}`);
    },

    addDrugToBranch(
        branchId: string,
        data: {
            branch_id: string;
            drug_id: string;
            quantity?: number;
            reserved_quantity?: number;
            location?: string | null;
            selling_price?: number | null;
        }
    ): Promise<BranchInventory> {
        return post<BranchInventory>(`${BASE}/branch/${branchId}/drugs`, data);
    },

    updateBranchDrug(
        branchId: string,
        drugId: string,
        data: { location?: string | null; selling_price?: number | null }
    ): Promise<BranchInventory> {
        return patch<BranchInventory>(`${BASE}/branch/${branchId}/drugs/${drugId}`, data);
    },

    updateBatch(batchId: string, data: DrugBatchUpdate): Promise<DrugBatch> {
        return patch<DrugBatch>(`${BASE}/batches/${batchId}`, data);
    },

    removeBranchDrug(branchId: string, drugId: string): Promise<void> {
        return del<void>(`${BASE}/branch/${branchId}/drugs/${drugId}`);
    },

    // ── Stock Adjustments ────────────────────────────────────────────────────

    /**
     * POST /inventory/adjust
     * Create a stock adjustment (damage, expired, theft, return, correction, transfer).
     * Note: for inter-branch transfers use `transfer()` instead.
     */
    adjust(data: StockAdjustmentCreate): Promise<StockAdjustmentResponse> {
        return post<StockAdjustmentResponse>(`${BASE}/adjust`, data);
    },

    /**
     * POST /inventory/transfer
     * Atomically move stock from one branch to another.
     */
    transfer(data: StockTransferCreate): Promise<StockTransferResponse> {
        return post<StockTransferResponse>(`${BASE}/transfer`, data);
    },

    // ── Drug Batches ─────────────────────────────────────────────────────────

    /**
     * GET /inventory/batches/drug/{drug_id}
     * Paginated FEFO-ordered batch list for a drug.
     * Optionally filtered to a specific branch.
     */
    getBatches(
        drugId: string,
        params: {
            page?: number;
            page_size?: number;
            branch_id?: string;
            include_expired?: boolean;
            include_empty?: boolean;
            expiring_within_days?: number;
        } = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<DrugBatch>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<DrugBatch>>(
            `${BASE}/batches/drug/${drugId}?${qs}`,
            { signal }
        );
    },

    /**
     * POST /inventory/batches
     * Record a new drug batch (received stock from supplier).
     * Side effect: increments BranchInventory.quantity on the server.
     */
    createBatch(data: DrugBatchCreate): Promise<DrugBatch> {
        return post<DrugBatch>(`${BASE}/batches`, data);
    },

    /**
     * POST /inventory/batches/{batch_id}/consume
     * Reduce a batch's remaining_quantity (used internally by the sales flow).
     * Quantity is passed as a query parameter per the router.
     */
    consumeFromBatch(batchId: string, quantity: number): Promise<DrugBatch> {
        return post<DrugBatch>(
            `${BASE}/batches/${batchId}/consume?quantity=${quantity}`
        );
    },

    // ── Reports ──────────────────────────────────────────────────────────────

    /**
     * GET /inventory/low-stock
     * Low-stock and out-of-stock report for an organisation.
     * branch_id is an optional query parameter, not a path segment.
     */
    getLowStock(branchId?: string): Promise<LowStockReport> {
        const qs = branchId ? `?branch_id=${branchId}` : "";
        return get<LowStockReport>(`${BASE}/low-stock${qs}`);
    },

    /**
     * GET /inventory/expiring/{branch_id}
     * Batches expiring within `daysThreshold` days.
     * Query parameter is `days_threshold` (not `days`).
     */
    getExpiring(branchId: string, daysThreshold = 90): Promise<ExpiringBatchReport> {
        return get<ExpiringBatchReport>(
            `${BASE}/expiring/${branchId}?days_threshold=${daysThreshold}`
        );
    },

    /**
     * GET /inventory/reports/valuation/{branch_id}
     * Cost vs selling-value breakdown for all stock at a branch.
     * Note the `/reports/` prefix in the path.
     */
    getValuation(branchId: string): Promise<InventoryValuationResponse> {
        return get<InventoryValuationResponse>(
            `${BASE}/reports/valuation/${branchId}`
        );
    },
};
