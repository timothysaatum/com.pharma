import { User } from "@/types";

/**
 * Hook for easy permission checking in components.
 */
export function usePermissions() {
    const checkPermission = (user: User | null, permission: string): boolean => {
        if (!user) return false;
        if (user.is_super_admin) return true;

        // Org Admin (no branch assignments means full org access)
        if (user.assigned_branches.length === 0) return true;

        return user.roles.some(role =>
            role.permissions.includes(permission) || role.permissions.includes("*")
        );
    };

    const hasAnyPermission = (user: User | null, permissions: string[]): boolean => {
        return permissions.some(p => checkPermission(user, p));
    };

    const hasAllPermissions = (user: User | null, permissions: string[]): boolean => {
        return permissions.every(p => checkPermission(user, p));
    };

    return { checkPermission, hasAnyPermission, hasAllPermissions };
}
