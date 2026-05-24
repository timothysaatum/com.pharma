import { post, get, patch } from "./client";
import type {
    AdminCreate,
    OnboardingRequest,
    OnboardingResponse,
    Organization,
    OrganizationStats,
    OrganizationUpdate,
    PaginatedResponse,
    BranchCreate,
    SubscriptionTier,
} from "@/types";
import type { OnboardingValues } from "@/lib/validators";

// ─────────────────────────────────────────────────────────────────────────────
// Settings update shape — mirrors OrganizationSettingsUpdate Pydantic schema
// ─────────────────────────────────────────────────────────────────────────────

export interface OrganizationSettingsUpdate {
    currency?: string;
    timezone?: string;
    low_stock_threshold?: number;
    enable_loyalty_program?: boolean;
    loyalty?: {
        points_per_unit?: number;
        tier_thresholds?: {
            silver?: number;
            gold?: number;
            platinum?: number;
        };
    };
    [key: string]: unknown;   // org settings are open-ended
}

// ─────────────────────────────────────────────────────────────────────────────
// Payload builder
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Maps validated onboarding form values to the exact OnboardingRequest
 * API payload.
 *
 * Key points:
 * - `admin` is typed as `AdminCreate` (not `UserCreate`), so `organization_id`
 *   is structurally excluded — the org doesn't exist at this point.
 * - `confirm_password` is a form-only field and is not sent to the API.
 * - Prefixed form fields (org_phone, admin_email, etc.) are mapped back to
 *   the flat API field names.
 * - `BranchAddress.country` has a Zod `.default("Ghana")` so it is always
 *   a string, satisfying the Pydantic `country: str` field.
 */
export function buildOnboardingPayload(values: OnboardingValues): OnboardingRequest {
    const clean = (v?: string) => (v === "" ? undefined : v);

    let branches: BranchCreate[] | undefined;
    if (values.branches && values.branches.length > 0) {
        branches = values.branches.map((b) => ({
            name: b.name,
            // FIX: code was silently dropped — now mapped to API payload.
            // BranchCreate.code is optional; the server auto-generates one
            // if omitted, but if the user typed one it must be sent.
            code: clean(b.code) ?? undefined,
            phone: clean(b.branch_phone),
            email: clean(b.branch_email),
            address: {
                street: b.address?.street,
                city: b.address?.city,
                state: b.address?.state,
                zip_code: b.address?.zip_code,
                country: b.address?.country ?? "Ghana",
            },
        }));
    }

    // AdminCreate has no organization_id — the org is created by the server
    const admin: AdminCreate = {
        username: values.username,
        email: values.admin_email,
        full_name: values.full_name,
        password: values.password,
        // confirm_password intentionally omitted
        phone: clean(values.admin_phone),
        employee_id: clean(values.employee_id),
        role: "admin",
    };

    return {
        name: values.name,
        type: values.type,
        license_number: clean(values.license_number),
        tax_id: clean(values.tax_id),
        phone: clean(values.org_phone),
        email: clean(values.org_email),
        subscription_tier: values.subscription_tier,
        currency: values.currency,
        timezone: values.timezone,
        address: values.address
            ? {
                street: values.address.street,
                city: values.address.city,
                state: values.address.state,
                zip: values.address.zip_code,
                country: values.address.country ?? "Ghana",
            }
            : undefined,
        admin,
        branches,
    };
}

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────

export const organizationApi = {
    /** POST /organizations/onboard  (super_admin only) */
    onboard: (data: OnboardingRequest): Promise<OnboardingResponse> =>
        post<OnboardingResponse>("/organizations/onboard", data),

    /** GET /organizations — paginated (super_admin / admin) */
    list: (
        page = 1,
        pageSize = 50,
        filters?: {
            search?: string;
            is_active?: boolean;
            subscription_tier?: string;
        },
        signal?: AbortSignal
    ): Promise<PaginatedResponse<Organization>> => {
        const params = new URLSearchParams({
            page: String(page),
            page_size: String(pageSize),
        });
        if (filters?.search) params.set("search", filters.search);
        if (filters?.is_active !== undefined)
            params.set("is_active", String(filters.is_active));
        if (filters?.subscription_tier)
            params.set("subscription_tier", filters.subscription_tier);
        return get<PaginatedResponse<Organization>>(
            `/organizations?${params.toString()}`,
            { signal }
        );
    },

    /** GET /organizations/{id} */
    getById: (id: string, signal?: AbortSignal): Promise<Organization> =>
        get<Organization>(`/organizations/${id}`, { signal }),

    /** PATCH /organizations/{id}  (admin or super_admin) */
    update: (id: string, data: OrganizationUpdate): Promise<Organization> =>
        patch<Organization>(`/organizations/${id}`, data),

    /** GET /organizations/{id}/stats */
    getStats: (id: string, signal?: AbortSignal): Promise<OrganizationStats> =>
        get<OrganizationStats>(`/organizations/${id}/stats`, { signal }),

    /** POST /organizations/{id}/activate  (super_admin only) */
    activate: (id: string): Promise<Organization> =>
        post<Organization>(`/organizations/${id}/activate`),

    /**
     * POST /organizations/{id}/deactivate  (super_admin only)
     * The router accepts an OrganizationActivationRequest body with `reason`.
     */
    deactivate: (id: string, reason?: string): Promise<Organization> =>
        post<Organization>(`/organizations/${id}/deactivate`, { reason }),

    /**
     * POST /organizations/{id}/subscription  (super_admin only)
     * Updates the subscription tier and extends the expiry by `extendMonths`.
     */
    updateSubscription: (
        id: string,
        tier: SubscriptionTier,
        extendMonths: number
    ): Promise<Organization> =>
        post<Organization>(`/organizations/${id}/subscription`, {
            subscription_tier: tier,
            extend_months: extendMonths,
        }),

    /**
     * PATCH /organizations/{id}/settings  (admin or super_admin)
     * Merges the supplied keys into the org's existing settings JSONB.
     */
    updateSettings: (
        id: string,
        settings: OrganizationSettingsUpdate
    ): Promise<Organization> =>
        patch<Organization>(`/organizations/${id}/settings`, settings),
};