/**
 * ============
 * HTTP wrappers for every sales endpoint used by the POS.
 * All URLs verified against sales_endpoints.py.
 */

import { get, post } from "./client";
import type {
    Sale,
    SaleCreate,
    SaleWithDetails,
    PaginatedResponse,
} from "@/types";

// ── Response shapes (matching sales_schemas.py exactly) ──────────────────────

export interface SaleItemResponse {
    id: string;
    sale_id: string;
    drug_id: string;
    drug_name: string;
    drug_sku: string | null;
    drug_generic_name: string | null;
    quantity: number;
    batch_id: string | null;
    batch_number: string | null;
    batch_expiry_date: string | null;
    unit_price: number;
    subtotal: number;
    contract_discount_percentage: number;
    contract_discount_amount: number;
    additional_discount_amount: number;
    total_discount_amount: number;
    tax_rate: number;
    tax_amount: number;
    total_price: number;
    applied_contract_id: string | null;
    applied_contract_name: string | null;
    insurance_covered: boolean;
    patient_copay: number | null;
    requires_prescription: boolean;
    prescription_verified: boolean;
    prescription_id: string | null;
    allergy_check_performed: boolean;
    created_at: string;
    updated_at: string;
}

export interface ProcessSaleResponse {
    sale: SaleWithDetails;
    inventory_updated: number;
    batches_updated: number;
    loyalty_points_awarded: number;
    loyalty_tier_upgraded: boolean;
    new_loyalty_tier: string | null;
    low_stock_alerts_created: number;
    expiry_alerts_created: number;
    contract_applied: string;
    contract_discount_given: number;
    estimated_savings: number;
    success: boolean;
    message: string;
    warnings: string[];
}

export interface RefundItemData {
    sale_item_id: string;
    quantity: number;
    reason: string;
    restock?: boolean;
}

export interface RefundSaleRequest {
    reason: string;
    items_to_refund: RefundItemData[];
    refund_amount: number;
    refund_method?: "original" | "cash" | "store_credit";
    manager_approval_user_id: string;
}

export interface RefundSaleResponse {
    sale: SaleWithDetails;
    refund_id: string;
    refund_amount: number;
    refund_method: string;
    inventory_restored: number;
    batches_restored: number;
    loyalty_points_deducted: number;
    success: boolean;
    message: string;
}

export interface CancelSaleRequest {
    reason: string;
    manager_approval_user_id: string;
    restore_inventory?: boolean;
}

export interface ReceiptData {
    receipt_number: string;
    receipt_date: string;
    organization: {
        name: string;
        tax_id: string | null;
        phone: string | null;
        email: string | null;
    };
    branch: {
        name: string;
        address: Record<string, unknown> | null;
        phone: string | null;
    };
    customer: {
        name: string | null;
        phone: string | null;
    };
    items: Array<{
        name: string;
        generic_name: string | null;
        quantity: number;
        unit_price: number;
        subtotal: number;
        contract_discount: number;
        additional_discount: number;
        total_discount: number;
        tax: number;
        total: number;
        batch_number: string | null;
        insurance_covered: boolean;
        patient_copay: number | null;
    }>;
    subtotal: number;
    contract_discount: number;
    additional_discount: number;
    total_discount: number;
    tax: number;
    total: number;
    amount_paid: number;
    change: number;
    payment_method: string;
    cashier: string | null;
    contract: {
        name: string;
        type: string;
        discount_percentage: number;
    } | null;
    insurance: {
        claim_number: string;
        preauth_number: string | null;
        patient_copay: number;
        insurance_covered: number;
        verified: boolean;
    } | null;
}

// ── API ───────────────────────────────────────────────────────────────────────

export const salesApi = {
    /**
     * POST /sales/
     * Process a complete sale. Requires process_sales permission.
     * Server handles: FEFO batch selection, pricing, inventory deduction,
     * contract validation, loyalty points, and low-stock alerts.
     */
    processSale: (data: SaleCreate): Promise<ProcessSaleResponse> =>
        post<ProcessSaleResponse>("/sales/", data),

    /**
     * GET /sales/{id}
     * Full sale details including all line items.
     */
    getById: (id: string, signal?: AbortSignal): Promise<SaleWithDetails> =>
        get<SaleWithDetails>(`/sales/${id}`, { signal }),

    /**
     * GET /sales/
     * Paginated sale history with filters.
     */
    list: (
        params: {
            page?: number;
            page_size?: number;
            branch_id?: string;
            start_date?: string;
            end_date?: string;
            status?: string;
            payment_method?: string;
            customer_id?: string;
            cashier_id?: string;
        } = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<Sale>> => {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<Sale>>(`/sales/?${qs}`, { signal });
    },

    /**
     * GET /sales/{id}/receipt
     * Formatted receipt data for printing or display.
     */
    getReceipt: (id: string, signal?: AbortSignal): Promise<ReceiptData> =>
        get<ReceiptData>(`/sales/${id}/receipt`, { signal }),

    /**
     * POST /sales/{id}/refund
     * Full or partial refund. Requires process_refunds permission (manager+).
     */
    refund: (id: string, data: RefundSaleRequest): Promise<RefundSaleResponse> =>
        post<RefundSaleResponse>(`/sales/${id}/refund`, data),

    /**
     * POST /sales/{id}/cancel
     * Cancel a draft sale only. Completed sales use refund.
     */
    cancel: (id: string, data: CancelSaleRequest): Promise<Sale> =>
        post<Sale>(`/sales/${id}/cancel`, data),
};