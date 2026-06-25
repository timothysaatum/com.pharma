import { User } from "@/types";

function userHasPermission(user: User, permission: string): boolean {
    if (user.is_super_admin) return true;
    // Use server-computed effective permissions (includes hierarchy inheritance).
    // The login endpoint doesn't compute _effective_permissions, so it returns
    // an empty default array [] — check length to distinguish "not computed" from
    // "computed but empty", and fall back to checking direct role permissions.
    const effective = user.effective_permissions?.effective_permissions;
    if (effective && effective.length > 0) {
        return effective.includes(permission) || effective.includes("*");
    }
    // Fallback: check assigned roles directly
    return user.roles.some(role =>
        role.permissions.includes(permission) || role.permissions.includes("*")
    );
}

/**
 * Hook for permission checking in components.
 * Uses server-computed effective permissions which respect role hierarchy:
 * a role at level N inherits all permissions from roles at level < N.
 */
export function usePermissions() {
    const checkPermission = (user: User | null, permission: string): boolean => {
        if (!user) return false;
        return userHasPermission(user, permission);
    };

    const hasAnyPermission = (user: User | null, permissions: string[]): boolean => {
        if (!user) return false;
        return permissions.some(p => userHasPermission(user, p));
    };

    const hasAllPermissions = (user: User | null, permissions: string[]): boolean => {
        if (!user) return false;
        return permissions.every(p => userHasPermission(user, p));
    };

    return { checkPermission, hasAnyPermission, hasAllPermissions };
}

/**
 * Standalone helpers for inline component usage (no hook needed).
 */
export function canUser(user: User | null, permission: string): boolean {
    if (!user) return false;
    return userHasPermission(user, permission);
}

export function canUserAny(user: User | null, permissions: string[]): boolean {
    if (!user) return false;
    return permissions.some(p => userHasPermission(user, p));
}
