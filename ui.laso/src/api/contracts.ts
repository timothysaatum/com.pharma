/**
 * ================
 * HTTP wrappers for all price contract endpoints.
 * Verified against price_contract_endpoints.py.
 *
 * Endpoints covered:
 *   POST   /contracts                        create
 *   GET    /contracts                        list (paginated + filtered)
 *   POST   /contracts/verify-eligibility     POS eligibility + verification token
 *   GET    /contracts/{id}                   get single
 *   GET    /contracts/{id}/details           get with insurance + branch details
 *   PATCH  /contracts/{id}                   update (partial)
 *   DELETE /contracts/{id}                   soft-delete (admin/super_admin only)
 *   POST   /contracts/{id}/approve           draft → active
 *   POST   /contracts/{id}/activate          suspended → active
 *   POST   /contracts/{id}/suspend           active → suspended
 *   POST   /contracts/{id}/duplicate         clone with new code + name
 *   GET    /contracts/available/{branch_id}  POS contract picker
 */

import { get, post, patch, del } from "./client";
import type { PriceContract } from "@/types";

// ── POS-specific shape ────────────────────────────────────────────────────────

export interface AvailableContract {
    id: string;
    code: string;
    name: string;
    type: ContractType;
    discount_percentage: number;
    is_default: boolean;
    requires_verification: boolean;
    requires_approval: boolean;
    display: string;
    warning: string | null;
    copay_amount: number | null;
    copay_percentage: number | null;
    requires_preauthorization: boolean;
    insurance_provider_id: string | null;
    daily_usage_limit: number | null;
    per_customer_usage_limit: number | null;
    applies_to_prescription_only: boolean;
    applies_to_otc: boolean;
    minimum_purchase_amount: number | null;
    maximum_purchase_amount: number | null;
}

export interface ContractEligibilityRequest {
    contract_id: string;
    customer_id?: string | null;
    drug_ids: string[];
    branch_id: string;
    sale_amount?: number | null;
}

export interface ContractEligibilityResponse {
    eligible: boolean;
    message: string;
    contract_name: string;
    discount_percentage: number;
    customer_eligible: boolean;
    customer_message?: string | null;
    branch_eligible: boolean;
    branch_message?: string | null;
    date_eligible: boolean;
    date_message?: string | null;
    amount_eligible: boolean;
    amount_message?: string | null;
    user_role_eligible: boolean;
    user_role_message?: string | null;
    eligible_drugs: Array<Record<string, unknown>>;
    ineligible_drugs: Array<Record<string, unknown>>;
    requires_verification: boolean;
    requires_approval: boolean;
    requires_preauthorization: boolean;
    insurance_details?: Record<string, unknown> | null;
    copay_amount?: number | null;
    copay_percentage?: number | null;
    verification_token?: string | null;
    verification_expires_at?: string | null;
}

// ── Contract response shape (full) ────────────────────────────────────────────

export type ContractType =
    | "insurance" | "corporate" | "staff"
    | "senior_citizen" | "standard" | "wholesale" | "promotional";

export type ContractStatus = "draft" | "active" | "suspended" | "expired" | "cancelled";

export interface ContractResponse extends PriceContract {
    requires_approval: boolean;
    daily_usage_limit: number | null;
    per_customer_usage_limit: number | null;
    requires_preauthorization: boolean;
    usage_count: number;
    total_sales_amount: number;
    total_discount_given: number;
    average_sale_amount: number;
    last_used_at: string | null;
    unique_customers_count: number;
    created_by: string;
    approved_by: string | null;
    approved_at: string | null;
    last_modified_by: string | null;
    last_modified_at: string | null;
}

export interface ContractWithDetails extends ContractResponse {
    insurance_provider_name: string | null;
    insurance_provider_code: string | null;
    creator_name: string | null;
    approver_name: string | null;
    applicable_branch_names: string[];
    custom_pricing_items_count: number;
    is_valid_today: boolean;
    days_until_expiry: number | null;
    is_expiring_soon: boolean;
}

export interface ContractListResponse {
    contracts: ContractResponse[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    total_active_contracts: number;
    total_suspended_contracts: number;
    total_expired_contracts: number;
}

// ── Create / Update payloads ──────────────────────────────────────────────────

export interface ContractCreate {
    organization_id: string;
    contract_code: string;
    contract_name: string;
    description?: string;
    contract_type: ContractType;
    is_default_contract: boolean;
    discount_type: "percentage" | "fixed_amount" | "custom";
    discount_percentage: number;
    applies_to_prescription_only: boolean;
    applies_to_otc: boolean;
    excluded_drug_categories?: string[];
    excluded_drug_ids?: string[];
    minimum_price_override?: number;
    maximum_discount_amount?: number;
    minimum_purchase_amount?: number;
    maximum_purchase_amount?: number;
    applies_to_all_branches: boolean;
    applicable_branch_ids?: string[];
    effective_from: string;   // ISO date string YYYY-MM-DD
    effective_to?: string;
    requires_verification: boolean;
    requires_approval: boolean;
    allowed_user_roles?: string[];
    daily_usage_limit?: number;
    per_customer_usage_limit?: number;
    insurance_provider_id?: string;
    copay_amount?: number;
    copay_percentage?: number;
    requires_preauthorization?: boolean;
    status?: ContractStatus;
    is_active?: boolean;
}

export interface ContractUpdate {
    contract_name?: string;
    description?: string;
    discount_percentage?: number;
    applies_to_prescription_only?: boolean;
    applies_to_otc?: boolean;
    excluded_drug_categories?: string[];
    excluded_drug_ids?: string[];
    minimum_price_override?: number;
    maximum_discount_amount?: number;
    minimum_purchase_amount?: number;
    maximum_purchase_amount?: number;
    applies_to_all_branches?: boolean;
    applicable_branch_ids?: string[];
    effective_to?: string;
    requires_verification?: boolean;
    requires_approval?: boolean;
    allowed_user_roles?: string[];
    daily_usage_limit?: number;
    per_customer_usage_limit?: number;
    copay_amount?: number;
    copay_percentage?: number;
    requires_preauthorization?: boolean;
    status?: ContractStatus;
    is_active?: boolean;
}

export interface ContractListParams {
    contract_type?: ContractType;
    status?: ContractStatus;
    is_active?: boolean;
    is_default?: boolean;
    branch_id?: string;
    search?: string;
    sort_by?: string;
    sort_order?: "asc" | "desc";
    page?: number;
    page_size?: number;
}

export interface ContractCodeAvailability {
    available: boolean;
    code: string;
}

// ── API ───────────────────────────────────────────────────────────────────────

export const contractsApi = {
    /**
     * GET /contracts
     * Paginated list with filters. Admin/manager/super_admin can see all;
     * other roles see only active contracts applicable to them.
     */
    list(params: ContractListParams = {}, signal?: AbortSignal): Promise<ContractListResponse> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<ContractListResponse>(`/contracts?${qs}`, { signal });
    },

    /** GET /contracts/{id} */
    getById(id: string, signal?: AbortSignal): Promise<ContractResponse> {
        return get<ContractResponse>(`/contracts/${id}`, { signal });
    },

    /** POST /contracts/verify-eligibility — POS contract validation */
    verifyEligibility(
        data: ContractEligibilityRequest,
        signal?: AbortSignal,
    ): Promise<ContractEligibilityResponse> {
        return post<ContractEligibilityResponse>("/contracts/verify-eligibility", data, { signal });
    },

    /** GET /contracts/{id}/details — with insurance provider + branch names */
    getDetails(id: string, signal?: AbortSignal): Promise<ContractWithDetails> {
        return get<ContractWithDetails>(`/contracts/${id}/details`, { signal });
    },

    /** POST /contracts — requires admin/manager/super_admin */
    create(data: ContractCreate): Promise<ContractResponse> {
        return post<ContractResponse>("/contracts", data);
    },

    /** PATCH /contracts/{id} — partial update, admin/manager/super_admin */
    update(id: string, data: ContractUpdate): Promise<ContractResponse> {
        return patch<ContractResponse>(`/contracts/${id}`, data);
    },

    /** DELETE /contracts/{id} — soft delete, admin/super_admin only */
    remove(id: string): Promise<{ message: string }> {
        return del<{ message: string }>(`/contracts/${id}`);
    },

    /** POST /contracts/{id}/approve — draft → active */
    approve(id: string, notes?: string): Promise<ContractResponse> {
        return post<ContractResponse>(`/contracts/${id}/approve`, { notes });
    },

    /** POST /contracts/{id}/activate — suspended → active */
    activate(id: string): Promise<ContractResponse> {
        return post<ContractResponse>(`/contracts/${id}/activate`, {});
    },

    /** POST /contracts/{id}/suspend — active → suspended */
    suspend(id: string, reason: string): Promise<ContractResponse> {
        return post<ContractResponse>(`/contracts/${id}/suspend`, { reason });
    },

    /** POST /contracts/{id}/duplicate — clone with new code + name */
    duplicate(id: string, newCode: string, newName: string): Promise<ContractResponse> {
        const qs = new URLSearchParams({ new_code: newCode, new_name: newName });
        return post<ContractResponse>(`/contracts/${id}/duplicate?${qs}`);
    },

    /**
     * GET /contracts/available/{branch_id}
     * Returns contracts for POS — filtered to active, valid today, role-allowed.
     */
    getAvailableForPos(branchId: string, signal?: AbortSignal): Promise<AvailableContract[]> {
        return get<AvailableContract[]>(`/contracts/available/${branchId}`, { signal });
    },

    /**
     * GET /contracts/check-code/{contract_code}
     * Check if a contract code is available (not already used).
     */
    checkCode(code: string): Promise<ContractCodeAvailability> {
        return get<ContractCodeAvailability>(`/contracts/check-code/${encodeURIComponent(code)}`);
    },
};
