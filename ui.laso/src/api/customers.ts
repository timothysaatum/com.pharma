/**
 * ================
 * HTTP wrappers for all customer endpoints.
 * Derived from customer_schemas.py + REST conventions.
 *
 * Endpoints covered:
 *   POST   /customers                    create
 *   GET    /customers                    list (paginated + filtered)
 *   GET    /customers/{id}              get with details
 *   PATCH  /customers/{id}              update
 *   DELETE /customers/{id}              soft-delete
 *   GET    /customers/search            quick lookup for POS
 *   POST   /customers/{id}/loyalty/award    award loyalty points
 *   POST   /customers/{id}/loyalty/deduct   deduct loyalty points
 */

import { get, post, patch, del } from "./client";
import type { Customer } from "@/types";

// ── Response shapes ───────────────────────────────────────────────────────────

export interface CustomerWithDetails extends Customer {
    insurance_provider_name: string | null;
    insurance_provider_code: string | null;
    preferred_contract_name: string | null;
    preferred_contract_discount: number | null;
    /**
     * Aliased from total_orders on the backend schema.
     * Use this for displaying purchase count in the UI.
     */
    total_purchases: number;
    /**
     * Aliased from total_value on the backend schema.
     * Use this for displaying total spend in the UI.
     */
    total_spent: number;
    last_purchase_date: string | null;
}

export interface CustomerQuickLookup {
    id: string;
    full_name: string | null;
    phone: string | null;
    email: string | null;
    customer_type: string;
    loyalty_points: number;
    loyalty_tier: string;
    has_insurance: boolean;
    insurance_provider_name: string | null;
    preferred_contract_name: string | null;
    eligible_for_senior_discount: boolean;
}

export interface CustomerSearchResult {
    matches: CustomerQuickLookup[];
    total: number;
    search_term: string;
}

export interface CustomerListResponse {
    customers: Customer[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
}

// ── Create / Update payloads ──────────────────────────────────────────────────

export interface CustomerCreate {
    organization_id: string;
    customer_type: "walk_in" | "registered" | "insurance" | "corporate";
    first_name?: string;
    last_name?: string;
    phone?: string;
    email?: string;
    date_of_birth?: string;   // ISO date YYYY-MM-DD
    address?: {
        street?: string;
        city?: string;
        state?: string;
        zip?: string;
        country?: string;
    };
    insurance_provider_id?: string;
    insurance_member_id?: string;
    insurance_card_image_url?: string;
    preferred_contract_id?: string;
    preferred_contact_method?: "email" | "phone" | "sms";
    marketing_consent?: boolean;
}

export interface CustomerUpdate {
    first_name?: string;
    last_name?: string;
    phone?: string;
    email?: string;
    date_of_birth?: string;
    address?: {
        street?: string;
        city?: string;
        state?: string;
        zip?: string;
        country?: string;
    };
    insurance_provider_id?: string;
    insurance_member_id?: string;
    insurance_card_image_url?: string;
    preferred_contract_id?: string;
    preferred_contact_method?: "email" | "phone" | "sms";
    marketing_consent?: boolean;
    is_active?: boolean;
}

export interface CustomerListParams {
    customer_type?: string;
    loyalty_tier?: string;
    is_active?: boolean;
    search?: string;
    insurance_provider_id?: string;
    preferred_contract_id?: string;
    sort_by?: string;
    sort_order?: "asc" | "desc";
    page?: number;
    page_size?: number;
}

// ── API ───────────────────────────────────────────────────────────────────────

export const customersApi = {
    /**
     * GET /customers
     * Paginated list. Supports search by name, phone, email, member ID.
     */
    list(params: CustomerListParams = {}, signal?: AbortSignal): Promise<CustomerListResponse> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<CustomerListResponse>(`/customers?${qs}`, { signal });
    },

    /** GET /customers/{id} — includes purchase stats and insurance/contract names */
    getById(id: string, signal?: AbortSignal): Promise<CustomerWithDetails> {
        return get<CustomerWithDetails>(`/customers/${id}`, { signal });
    },

    /**
     * GET /customers/search?q=...
     * Quick lookup for POS — returns minimal shape for fast typeahead.
     */
    search(query: string, signal?: AbortSignal): Promise<CustomerSearchResult> {
        return get<CustomerSearchResult>(
            `/customers/search?q=${encodeURIComponent(query)}`,
            { signal }
        );
    },

    /** POST /customers — create new customer */
    create(data: CustomerCreate): Promise<CustomerWithDetails> {
        return post<CustomerWithDetails>("/customers", data);
    },

    /** PATCH /customers/{id} — partial update */
    update(id: string, data: CustomerUpdate): Promise<CustomerWithDetails> {
        return patch<CustomerWithDetails>(`/customers/${id}`, data);
    },

    /** DELETE /customers/{id} — soft delete */
    remove(id: string): Promise<{ message: string }> {
        return del<{ message: string }>(`/customers/${id}`);
    },

    /** POST /customers/{id}/loyalty/award — manually award points (manager+) */
    awardPoints(id: string, points: number, reason: string): Promise<CustomerWithDetails> {
        return post<CustomerWithDetails>(`/customers/${id}/loyalty/award`, { points, reason });
    },

    /** POST /customers/{id}/loyalty/deduct — manually deduct points (manager+) */
    deductPoints(id: string, points: number, reason: string): Promise<CustomerWithDetails> {
        return post<CustomerWithDetails>(`/customers/${id}/loyalty/deduct`, { points, reason });
    },
};