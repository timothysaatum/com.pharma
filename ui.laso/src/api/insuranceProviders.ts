/**
 * Insurance Provider API Client
 *
 * HTTP wrappers for all insurance provider endpoints.
 * Provides CRUD operations for managing insurance providers.
 *
 * Endpoints covered:
 *   POST   /insurance-providers               create
 *   GET    /insurance-providers               list (paginated)
 *   GET    /insurance-providers/search        search (for dropdowns)
 *   GET    /insurance-providers/{id}          get single
 *   PATCH  /insurance-providers/{id}          update
 *   POST   /insurance-providers/{id}/deactivate
 *   POST   /insurance-providers/{id}/activate
 *   DELETE /insurance-providers/{id}          delete
 */

import { get, post, patch, del } from "./client";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface InsuranceProviderAddress {
    street?: string;
    city?: string;
    state?: string;
    postal_code?: string;
    country?: string;
}

export interface InsuranceProvider {
    id: string;
    organization_id: string;
    name: string;
    code: string;
    logo_url?: string;
    phone?: string;
    email?: string;
    website?: string;
    address?: InsuranceProviderAddress;
    primary_contact_name?: string;
    primary_contact_phone?: string;
    primary_contact_email?: string;
    billing_cycle: "daily" | "weekly" | "monthly" | "quarterly" | "annually";
    payment_terms: "NET15" | "NET30" | "NET60" | "COD" | "PREPAID";
    requires_card_verification: boolean;
    requires_preauth: boolean;
    verification_endpoint?: string;
    is_active: boolean;
    notes?: string;
    created_at: string;
    updated_at: string;
    sync_status: string;
    sync_version: number;
}

export interface InsuranceProviderSearchItem {
    id: string;
    name: string;
    code: string;
    logo_url?: string;
    is_active: boolean;
}

export interface InsuranceProviderListResponse {
    providers: InsuranceProvider[];
    total: number;
    page: number;
    page_size: number;
}

// ── Create / Update payloads ──────────────────────────────────────────────────

export interface InsuranceProviderCreate {
    name: string;
    code: string;
    logo_url?: string;
    phone?: string;
    email?: string;
    website?: string;
    address?: InsuranceProviderAddress;
    primary_contact_name?: string;
    primary_contact_phone?: string;
    primary_contact_email?: string;
    billing_cycle?: "daily" | "weekly" | "monthly" | "quarterly" | "annually";
    payment_terms?: "NET15" | "NET30" | "NET60" | "COD" | "PREPAID";
    requires_card_verification?: boolean;
    requires_preauth?: boolean;
    verification_endpoint?: string;
    is_active?: boolean;
    notes?: string;
}

export interface InsuranceProviderUpdate {
    name?: string;
    logo_url?: string;
    phone?: string;
    email?: string;
    website?: string;
    address?: InsuranceProviderAddress;
    primary_contact_name?: string;
    primary_contact_phone?: string;
    primary_contact_email?: string;
    billing_cycle?: "daily" | "weekly" | "monthly" | "quarterly" | "annually";
    payment_terms?: "NET15" | "NET30" | "NET60" | "COD" | "PREPAID";
    requires_card_verification?: boolean;
    requires_preauth?: boolean;
    verification_endpoint?: string;
    is_active?: boolean;
    notes?: string;
}

// ── API Methods ───────────────────────────────────────────────────────────────

const BASE = "/insurance-providers";

export const insuranceProvidersApi = {
    /**
     * Create a new insurance provider
     */
    create: async (data: InsuranceProviderCreate): Promise<InsuranceProvider> => {
        return post(`${BASE}/`, data);
    },

    /**
     * Get paginated list of insurance providers
     */
    list: async (
        skip: number = 0,
        limit: number = 50,
        activeOnly: boolean = false
    ): Promise<InsuranceProviderListResponse> => {
        const params = new URLSearchParams();
        params.append("skip", String(skip));
        params.append("limit", String(limit));
        params.append("active_only", String(activeOnly));
        return get(`${BASE}?${params.toString()}`);
    },

    /**
     * Search insurance providers (for dropdowns/autocomplete)
     */
    search: async (
        query: string = "",
        activeOnly: boolean = true
    ): Promise<InsuranceProviderSearchItem[]> => {
        const params = new URLSearchParams();
        params.append("q", query);
        params.append("active_only", String(activeOnly));
        return get(`${BASE}/search?${params.toString()}`);
    },

    /**
     * Get single insurance provider
     */
    getById: async (id: string): Promise<InsuranceProvider> => {
        return get(`${BASE}/${id}`);
    },

    /**
     * Update insurance provider
     */
    update: async (id: string, data: InsuranceProviderUpdate): Promise<InsuranceProvider> => {
        return patch(`${BASE}/${id}`, data);
    },

    /**
     * Deactivate an insurance provider
     */
    deactivate: async (id: string): Promise<InsuranceProvider> => {
        return post(`${BASE}/${id}/deactivate`, {});
    },

    /**
     * Activate a deactivated insurance provider
     */
    activate: async (id: string): Promise<InsuranceProvider> => {
        return post(`${BASE}/${id}/activate`, {});
    },

    /**
     * Delete (soft delete) an insurance provider
     */
    delete: async (id: string): Promise<void> => {
        return del(`${BASE}/${id}`);
    },
};
