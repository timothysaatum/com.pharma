import { get, post, patch, del } from "./client";
import type {
    UserResponse,
    UserCreate,
    UserUpdate,
    UserRole,
    PaginatedResponse,
} from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// Filter params
// ─────────────────────────────────────────────────────────────────────────────

export interface UserFilters {
    page?: number;
    page_size?: number;
    search?: string;
    role?: UserRole | "";
    is_active?: boolean | null;
    branch_id?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────

export const usersApi = {
    /**
     * GET /users
     * List users in the organization with optional filters.
     */
    list(filters: UserFilters = {}, signal?: AbortSignal): Promise<PaginatedResponse<UserResponse>> {
        const params: Record<string, unknown> = {};
        if (filters.page) params.page = filters.page;
        if (filters.page_size) params.page_size = filters.page_size;
        if (filters.search) params.search = filters.search;
        if (filters.role) params.role = filters.role;
        if (filters.branch_id) params.branch_id = filters.branch_id;
        if (filters.is_active !== null && filters.is_active !== undefined) {
            params.is_active = filters.is_active;
        }
        return get<PaginatedResponse<UserResponse>>("/users", { params, signal });
    },

    /**
     * GET /users/:id
     */
    getById(userId: string, signal?: AbortSignal): Promise<UserResponse> {
        return get<UserResponse>(`/users/${userId}`, { signal });
    },

    /**
     * POST /users
     * Create a new user (admin/super_admin only).
     */
    create(data: UserCreate): Promise<UserResponse> {
        return post<UserResponse>("/users", data);
    },

    /**
     * PATCH /users/:id
     * Partial update — only sends changed fields.
     */
    update(userId: string, data: UserUpdate): Promise<UserResponse> {
        return patch<UserResponse>(`/users/${userId}`, data);
    },

    /**
     * POST /users/:id/activate
     */
    activate(userId: string): Promise<UserResponse> {
        return post<UserResponse>(`/users/${userId}/activate`);
    },

    /**
     * POST /users/:id/deactivate
     */
    deactivate(userId: string): Promise<UserResponse> {
        return post<UserResponse>(`/users/${userId}/deactivate`);
    },

    /**
     * POST /users/:id/unlock
     * Clears an account lockout caused by too many failed login attempts.
     */
    unlock(userId: string): Promise<UserResponse> {
        return post<UserResponse>(`/users/${userId}/unlock`);
    },

    /**
     * DELETE /users/:id
     * Soft-delete (data is retained for audit).
     */
    remove(userId: string): Promise<void> {
        return del<void>(`/users/${userId}`);
    },
};