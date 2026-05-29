import type { UserRole } from "@/types";

export const SETTINGS_TABS = ["organization", "branches"] as const;
export type SettingsTabId = (typeof SETTINGS_TABS)[number];

export const ADMIN_TABS = ["drugs", "inventory", "purchases", "contracts", "prescriptions"] as const;
export type AdminTabId = (typeof ADMIN_TABS)[number];

export function getHomePathForRole(role?: UserRole | string | null): string {
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
