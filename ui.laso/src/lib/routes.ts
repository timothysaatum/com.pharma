
export const SETTINGS_TABS = ["organization", "branches", "roles"] as const;
export type SettingsTabId = (typeof SETTINGS_TABS)[number];

export const ADMIN_TABS = ["drugs", "inventory", "purchases", "contracts", "prescriptions"] as const;
export type AdminTabId = (typeof ADMIN_TABS)[number];

import { User } from "@/types";

export function getHomePath(user: User | null): string {
    if (!user) return "/login";
    if (user.is_super_admin) return "/admin";

    // Org Admins (no branch assignments) go to dashboard
    if (user.assigned_branches.length === 0) return "/admin";

    // If user has management roles, go to admin dashboard
    const managementRoles = ["Admin", "Manager", "Pharmacist"];
    const hasManagementRole = user.roles.some(r => managementRoles.includes(r.name));

    if (hasManagementRole) {
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
