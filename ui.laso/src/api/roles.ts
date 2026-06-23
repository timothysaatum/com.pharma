import { get, post, put, del } from "./client";
import { Role, RoleCreate, RoleUpdate, PermissionInfo } from "../types";

export const rolesApi = {
    /**
     * GET /roles
     * List all roles for the organization
     */
    getRoles(signal?: AbortSignal): Promise<Role[]> {
        return get<Role[]>("/roles", { signal });
    },

    /**
     * GET /roles/permissions
     * List all available system permissions
     */
    getPermissions(signal?: AbortSignal): Promise<PermissionInfo[]> {
        return get<PermissionInfo[]>("/roles/permissions", { signal });
    },

    /**
     * POST /roles
     * Create a new role
     */
    createRole(data: RoleCreate, signal?: AbortSignal): Promise<Role> {
        return post<Role>("/roles", data, { signal });
    },

    /**
     * GET /roles/{id}
     * Get role by ID
     */
    getRole(id: string, signal?: AbortSignal): Promise<Role> {
        return get<Role>(`/roles/${id}`, { signal });
    },

    /**
     * PUT /roles/{id}
     * Update a role
     */
    updateRole(id: string, data: RoleUpdate, signal?: AbortSignal): Promise<Role> {
        return put<Role>(`/roles/${id}`, data, { signal });
    },

    /**
     * DELETE /roles/{id}
     * Delete a role
     */
    deleteRole(id: string, signal?: AbortSignal): Promise<void> {
        return del<void>(`/roles/${id}`, { signal });
    }
};
