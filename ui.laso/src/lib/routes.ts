
export const SETTINGS_TABS = ["organization", "branches", "roles", "security"] as const;
export type SettingsTabId = (typeof SETTINGS_TABS)[number];

export const ADMIN_TABS = ["drugs", "inventory", "purchases", "contracts", "prescriptions"] as const;
export type AdminTabId = (typeof ADMIN_TABS)[number];

import { User } from "@/types";

export function getHomePath(user: User | null): string {
    if (!user) return "/login";
    if (user.is_super_admin) return "/admin";

    // Org Admins (no branch assignments) go to dashboard
    if (user.assigned_branches.length === 0) return "/admin";

    // Use hierarchical role level: roles at level >= 20 are management
    // Login endpoint returns max_role_level=0 (default), so fall back to
    // computing from assigned roles when the effective value is falsy (0).
    const effectiveLevel = user.effective_permissions?.max_role_level;
    const maxLevel = effectiveLevel || Math.max(...user.roles.map(r => r.level), 0);

    if (maxLevel >= 20) {
        return "/admin";
    }

    return "/pos";
}

// Deprecated: use getHomePath(user) instead
export function getHomePathForRole(role?: string | null, isSuperAdmin?: boolean): string {
    if (isSuperAdmin) return "/admin";
    if (role === "admin" || role === "super_admin" || role === "manager") {
        return "/admin";
    }
    return "/pos";
}

export function parseSettingsTab(tab?: string): SettingsTabId {
    return SETTINGS_TABS.includes(tab as SettingsTabId)
        ? (tab as SettingsTabId)
        : "organization";
}

export function parseAdminTab(tab?: string): AdminTabId {
    return ADMIN_TABS.includes(tab as AdminTabId)
        ? (tab as AdminTabId)
        : "drugs";
}
