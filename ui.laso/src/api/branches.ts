import { get, post, patch, del } from "./client";
import type {
    Branch,
    BranchCreate,
    BranchUpdate,
    BranchListItem,
    PaginatedResponse,
} from "@/types";

// ── Extra response shapes not in index.ts ────────────────────────────────────

export interface BranchWithStats extends Branch {
    total_inventory_items: number;
    total_inventory_value: number;
    low_stock_count: number;
    total_sales_today: number;
    total_sales_month: number;
    active_users_count: number;
}

export interface BranchUserSummary {
    id: string;
    username: string;
    full_name: string;
    email: string;
    role: string;
    is_active: boolean;
}

export interface BranchAssignmentResult {
    success: boolean;
    message: string;
    user_id: string;
    assigned_branches: string[];
}

export interface BranchSearchFilters {
    search?: string;
    is_active?: boolean;
    manager_id?: string;
    state?: string;
    city?: string;
}

export interface PaginationParams {
    page?: number;
    page_size?: number;
}

// ── API ───────────────────────────────────────────────────────────────────────

export const branchApi = {
    // ── Queries ──────────────────────────────────────────────────────────────

    /**
     * GET /branches/my-branches
     * Branches the current user is assigned to.
     * Used post-login to determine single vs multi-branch flow.
     */
    listMine(signal?: AbortSignal): Promise<BranchListItem[]> {
        return get<BranchListItem[]>("/branches/my-branches", { signal });
    },

    /**
     * GET /branches
     * Paginated list of all branches in the organisation.
     * Supports search, is_active, manager_id, state, city filters.
     */
    list(
        params: BranchSearchFilters & PaginationParams = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<BranchListItem>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<BranchListItem>>(`/branches?${qs}`, { signal });
    },

    /**
     * GET /branches/{id}
     * Full branch response including operating_hours and sync fields.
     */
    getById(id: string, signal?: AbortSignal): Promise<Branch> {
        return get<Branch>(`/branches/${id}`, { signal });
    },

    /**
     * GET /branches/code/{code}
     * Quick lookup by the branch's unique code (e.g. "MAIN", "BR-001").
     */
    getByCode(code: string, signal?: AbortSignal): Promise<Branch> {
        return get<Branch>(`/branches/code/${code}`, { signal });
    },

    /**
     * GET /stats/{branch_id}
     * Branch details with computed stats: inventory value, sales today/month,
     * low stock count, active users count.
     */
    getWithStats(branchId: string, signal?: AbortSignal): Promise<BranchWithStats> {
        return get<BranchWithStats>(`/stats/${branchId}`, { signal });
    },

    /**
     * GET /branches/{id}/users
     * All users that have access to this branch.
     */
    getUsers(branchId: string, signal?: AbortSignal): Promise<BranchUserSummary[]> {
        return get<BranchUserSummary[]>(`/branches/${branchId}/users`, { signal });
    },

    // ── Mutations ─────────────────────────────────────────────────────────────

    /**
     * POST /branches
     * Creates a new branch. Requires admin or super_admin role.
     * organization_id is optional — if omitted, defaults to the current user's org.
     */
    create(data: BranchCreate): Promise<Branch> {
        return post<Branch>("/branches", data);
    },

    /**
     * PATCH /branches/{id}
     * Partial update — only supplied fields are changed.
     * Requires admin or super_admin role.
     */
    update(id: string, data: BranchUpdate): Promise<Branch> {
        return patch<Branch>(`/branches/${id}`, data);
    },

    /**
     * DELETE /branches/{id}
     * Soft-deletes by default. Pass hardDelete=true to permanently remove.
     * Blocked if the branch has existing inventory or recent sales.
     */
    remove(id: string, hardDelete = false): Promise<void> {
        return del<void>(`/branches/${id}?hard_delete=${hardDelete}`);
    },

    /**
     * POST /branches/{id}/activate
     * Re-enable a previously deactivated branch.
     */
    activate(id: string): Promise<Branch> {
        return post<Branch>(`/branches/${id}/activate`);
    },

    /**
     * POST /branches/{id}/deactivate
     * Temporarily close a branch without deleting it.
     */
    deactivate(id: string): Promise<Branch> {
        return post<Branch>(`/branches/${id}/deactivate`);
    },

    /**
     * POST /branches/assign-user
     * Assign a user to one or more branches.
     * This *replaces* existing branch assignments for that user.
     */
    assignUser(userId: string, branchIds: string[]): Promise<BranchAssignmentResult> {
        return post<BranchAssignmentResult>("/branches/assign-user", {
            user_id: userId,
            branch_ids: branchIds,
        });
    },

    /**
     * POST /branches/search
     * Advanced search via POST body for complex filter combinations.
     */
    searchAdvanced(
        filters: BranchSearchFilters,
        params: PaginationParams = {}
    ): Promise<PaginatedResponse<BranchListItem>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined) qs.set(k, String(v));
        });
        const url = qs.toString() ? `/branches/search?${qs}` : "/branches/search";
        return post<PaginatedResponse<BranchListItem>>(url, filters);
    },
};