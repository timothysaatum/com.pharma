import { useState, useEffect, useRef } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
    ShoppingCart, Users, BarChart2, FileText,
    LogOut, Building2, Menu, X, Activity,
    Receipt, Settings, UserCog, Cog, ChevronDown, Check, ShieldAlert,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import packageMetadata from "../../../package.json";
import { branchApi } from "@/api/branches";
import type { BranchListItem } from "@/types";
import { organizationApi } from "@/api/organization";
import { offlineCache } from "@/lib/storage";
import { APP_NAME } from "@/lib/appConfig";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { SyncIndicator } from "@/components/layout/SyncIndicator";
import { usePermissions } from "@/hooks/usePermissions";

const IS_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

interface NavItem {
    to: string;
    icon: React.ElementType;
    label: string;
    roles?: string[];
}

export const NAV_ITEMS: NavItem[] = [
    { to: "/pos", icon: ShoppingCart, label: "Point of Sale" },
    { to: "/sales", icon: Receipt, label: "Sales History" },
    { to: "/customers", icon: Users, label: "Customers" },
    { to: "/prescriptions", icon: FileText, label: "Prescriptions" },
    {
        to: "/reports",
        icon: BarChart2,
        label: "Reports",
        roles: ["admin", "manager", "super_admin"],
    },
    {
        to: "/admin",
        icon: Cog,
        label: "Admin",
        roles: ["admin", "super_admin", "manager"],
    },
    {
        to: "/users",
        icon: UserCog,
        label: "Users",
        roles: ["admin", "super_admin", "manager"],
    },
    {
        to: "/settings",
        icon: Settings,
        label: "Settings",
        roles: ["admin", "super_admin"],
    },
    {
        to: "/audit-logs",
        icon: Activity,
        label: "Audit Logs",
        roles: ["admin", "manager", "super_admin"],
    },
];


export function AppShell({ children }: { children: React.ReactNode }) {
    const { user, logout, activeBranchId, setActiveBranch } = useAuthStore();
    const navigate = useNavigate();
    const [collapsed, setCollapsed] = useState(false);
    const [loggingOut, setLoggingOut] = useState(false);
    const [branchName, setBranchName] = useState<string | null | undefined>(undefined);
    const [organizationName, setOrganizationName] = useState<string | null | undefined>(undefined);
    const [version, setVersion] = useState<string>("");

    useEffect(() => {
        const fetchVersion = async () => {
            if (!IS_TAURI) {
                setVersion(packageMetadata.version);
                return;
            }
            try {
                const { getVersion } = await import(/* @vite-ignore */ "@tauri-apps/api/app");
                const v = await getVersion();
                setVersion(v);
            } catch (err) {
                console.error("Failed to fetch app version:", err);
                setVersion(packageMetadata.version);
            }
        };
        fetchVersion();
    }, []);

    // ── Branch switcher state ──────────────────────────────────────────────────
    const [branches, setBranches] = useState<BranchListItem[]>([]);
    const [isBranchSwitcherOpen, setIsBranchSwitcherOpen] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!user) {
            setBranches([]);
            return;
        }

        let cancelled = false;
        const assignedIds = new Set((user.assigned_branches ?? []).map(String));
        const canSeeAllBranches =
            user.is_super_admin ||
            assignedIds.size === 0 ||
            user.roles?.some((role) =>
                role.permissions.includes("*") ||
                role.permissions.includes("manage_branches") ||
                role.permissions.includes("manage_organization")
            );

        void (async () => {
            const cached = await offlineCache.getBranches({ allowExpired: true });
            if (!cancelled && cached?.length) {
                setBranches(
                    canSeeAllBranches
                        ? cached
                        : cached.filter((branch) => assignedIds.has(String(branch.id)))
                );
            }

            try {
                const fresh = canSeeAllBranches
                    ? (await branchApi.list({ is_active: true, page_size: 100 })).items
                    : await branchApi.listMine();

                if (cancelled) return;
                const visible = canSeeAllBranches
                    ? fresh
                    : fresh.filter((branch) => assignedIds.has(String(branch.id)));

                setBranches(visible);
                await offlineCache.setBranches(visible);

                if (!activeBranchId && visible.length === 1) {
                    setActiveBranch(String(visible[0].id));
                }
            } catch {
                if (!cancelled && !cached?.length) {
                    setBranches([]);
                }
            }
        })();

        return () => { cancelled = true; };
    }, [activeBranchId, setActiveBranch, user]);

    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
                setIsBranchSwitcherOpen(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    useEffect(() => {
        if (!activeBranchId) {
            setBranchName(null);
            return;
        }
        let cancelled = false;
        setBranchName(undefined);
        branchApi
            .getById(activeBranchId)
            .then((b) => { if (!cancelled) setBranchName(b.name); })
            .catch(async () => {
                const cachedName = await offlineCache.getBranchName(activeBranchId);
                if (!cancelled) setBranchName(cachedName);
            });
        return () => { cancelled = true; };
    }, [activeBranchId]);

    useEffect(() => {
        const orgId = user?.organization_id;
        if (!orgId) {
            setOrganizationName(null);
            return;
        }

        let cancelled = false;
        setOrganizationName(undefined);

        void (async () => {
            const cachedOrg = await offlineCache.getOrganization({ allowExpired: true });
            if (!cancelled && cachedOrg?.id === orgId) {
                setOrganizationName(cachedOrg.name);
            }

            try {
                const org = await organizationApi.getById(orgId);
                if (!cancelled) {
                    setOrganizationName(org.name);
                    await offlineCache.setOrganization(org);
                }
            } catch {
                if (!cancelled && cachedOrg?.id !== orgId) {
                    setOrganizationName(null);
                }
            }
        })();

        return () => { cancelled = true; };
    }, [user?.organization_id]);

    const handleLogout = async () => {
        setLoggingOut(true);
        await logout();
        navigate("/login", { replace: true });
    };

    const { checkPermission } = usePermissions();

    const visibleNav = NAV_ITEMS.filter((item) => {
        // If no role restriction, everyone can see it
        if (!item.roles) return true;

        // Super admin has access to everything
        if (user?.is_super_admin) return true;

        // Map legacy roles to permissions for sidebar visibility
        // If the item had roles: ["admin", "manager"], we check for MANAGE_ORGANIZATION or VIEW_REPORTS
        const permissionMap: Record<string, string> = {
            "/reports": "view_reports",
            "/admin": "manage_inventory", // Admin link usually goes to drugs/inventory
            "/users": "manage_users",
            "/settings": "manage_organization",
            "/audit-logs": "view_audit_logs"
        };

        const requiredPermission = permissionMap[item.to];
        if (requiredPermission) {
            return checkPermission(user, requiredPermission);
        }

        // Fallback: If we don't have a specific permission mapping, use the old logic
        // but allow branch-assigned users if they are super admins (handled above)
        // or if they are org admins (no branch assignments).
        return user?.assigned_branches?.length === 0;
    });

    const initials = user?.full_name
        ? user.full_name.split(" ").map((n) => n[0]).join("").slice(0, 2).toUpperCase()
        : "??";

    const { status } = useSyncStatus();

    return (
        <div className="flex h-screen bg-surface overflow-hidden">
            {/* ── Sidebar ─────────────────────────────────────────── */}
            <aside
                className={`flex flex-col bg-ink text-white transition-[width] duration-200 flex-shrink-0 ${collapsed ? "w-14" : "w-56"
                    }`}
            >
                {/* Header */}
                <div className="flex items-center h-14 px-3 border-b border-white/10 gap-2">
                    <div className="w-7 h-7 rounded-lg bg-brand-500 flex items-center justify-center flex-shrink-0">
                        <img
                            src="/logo.png"
                            alt={`${organizationName ?? APP_NAME} logo`}
                            className="w-full h-full rounded-lg object-contain"
                        />
                    </div>
                    {!collapsed && (
                        <span className="font-display font-bold text-sm flex-1 truncate">
                            {organizationName ?? APP_NAME}
                        </span>
                    )}
                    <button
                        onClick={() => setCollapsed((c) => !c)}
                        className={`w-7 h-7 flex items-center justify-center rounded-lg text-white/50 hover:text-white hover:bg-white/10 transition-colors flex-shrink-0 ${collapsed ? "mx-auto" : ""
                            }`}
                    >
                        {collapsed ? <Menu className="w-4 h-4" /> : <X className="w-4 h-4" />}
                    </button>
                </div>

                {/* Branch switcher / pill */}
                {!collapsed && (activeBranchId || branches.length > 0) && (
                    <div className="relative mx-3 mt-3" ref={dropdownRef}>
                        <button
                            onClick={() => branches.length > 0 && setIsBranchSwitcherOpen(!isBranchSwitcherOpen)}
                            className={`w-full px-2.5 py-2 rounded-lg bg-white/5 border border-white/10 flex items-center gap-2 transition-colors ${branches.length > 0 ? "hover:bg-white/10 cursor-pointer text-left" : "cursor-default"
                                }`}
                        >
                            <Building2 className="w-3.5 h-3.5 text-white/40 flex-shrink-0" />
                            <div className="flex-1 min-w-0">
                                <p className="text-[10px] font-bold text-white/30 uppercase tracking-widest leading-none mb-1">Active Branch</p>
                                <p className="text-xs text-white/70 truncate font-semibold">
                                    {branchName === undefined ? "Loading…" : branchName ?? "Select Branch"}
                                </p>
                            </div>
                            {branches.length > 0 && (
                                <ChevronDown className={`w-3.5 h-3.5 text-white/30 transition-transform ${isBranchSwitcherOpen ? "rotate-180" : ""}`} />
                            )}
                        </button>

                        {/* Dropdown Menu */}
                        {isBranchSwitcherOpen && (
                            <div className="absolute left-0 right-0 mt-1 z-50 rounded-xl bg-ink border border-white/10 shadow-2xl overflow-hidden py-1.5">
                                <p className="px-3 py-1.5 text-[10px] font-bold text-white/30 uppercase tracking-widest border-b border-white/5 mb-1.5">Switch Branch</p>
                                <div className="max-h-64 overflow-y-auto">
                                    {branches.map((b) => (
                                        <button
                                            key={b.id}
                                            onClick={() => {
                                                setActiveBranch(b.id);
                                                setIsBranchSwitcherOpen(false);
                                            }}
                                            className="w-full px-3 py-2 text-left flex items-center justify-between hover:bg-white/5 transition-colors group"
                                        >
                                            <div className="min-w-0">
                                                <p className={`text-sm truncate ${activeBranchId === b.id ? "text-brand-400 font-bold" : "text-white/70 font-medium"}`}>
                                                    {b.name}
                                                </p>
                                                <p className="text-[10px] text-white/30 font-mono">{b.code}</p>
                                            </div>
                                            {activeBranchId === b.id && (
                                                <Check className="w-4 h-4 text-brand-400 flex-shrink-0" />
                                            )}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {/* Collapsed branch dot */}
                {collapsed && activeBranchId && (
                    <div className="flex justify-center mt-3">
                        <div
                            className="w-2 h-2 rounded-full bg-brand-400"
                            title={branchName ?? "Branch active"}
                        />
                    </div>
                )}

                {/* Navigation */}
                <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
                    {visibleNav.map((item) => {
                        const Icon = item.icon;
                        return (
                            <NavLink
                                key={item.to}
                                to={item.to}
                                title={collapsed ? item.label : undefined}
                                className={({ isActive }) =>
                                    `flex items-center gap-3 px-2.5 py-2 rounded-lg text-sm font-medium transition-colors ${isActive
                                        ? "bg-brand-500 text-white"
                                        : "text-white/55 hover:text-white hover:bg-white/10"
                                    } ${collapsed ? "justify-center px-0" : ""}`
                                }
                            >
                                <Icon className="w-4 h-4 flex-shrink-0" />
                                {!collapsed && <span className="truncate">{item.label}</span>}
                            </NavLink>
                        );
                    })}
                </nav>

                {/* Sync status */}
                <SyncIndicator collapsed={collapsed} />

                {/* App Version */}
                <div className={`px-4 py-2 border-t border-white/5 ${collapsed ? "flex justify-center" : ""}`}>
                    <p className={`text-[10px] font-mono text-white/20 uppercase tracking-widest ${collapsed ? "rotate-90 origin-center whitespace-nowrap my-4" : ""}`}>
                        v{version}
                    </p>
                </div>

                {/* User footer */}
                <div className="border-t border-white/10 p-2">
                    {!collapsed ? (
                        <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg">
                            <div className="w-7 h-7 rounded-full bg-brand-500/25 flex items-center justify-center flex-shrink-0">
                                <span className="text-xs font-bold text-brand-300">{initials}</span>
                            </div>
                            <div className="flex-1 min-w-0">
                                <p className="text-xs font-semibold text-white truncate leading-tight">
                                    {user?.full_name}
                                </p>
                                <p className="text-xs text-white/40 truncate leading-tight">
                                    {user?.is_super_admin ? "Super Admin" : user?.roles?.map(r => r.name).join(", ") || "Staff"}
                                </p>
                            </div>
                            <button
                                onClick={handleLogout}
                                disabled={loggingOut}
                                title="Log out"
                                className="w-7 h-7 flex items-center justify-center rounded-lg text-white/40 hover:text-red-400 hover:bg-white/10 transition-colors disabled:opacity-40 flex-shrink-0"
                            >
                                <LogOut className="w-3.5 h-3.5" />
                            </button>
                        </div>
                    ) : (
                        <button
                            onClick={handleLogout}
                            disabled={loggingOut}
                            title="Log out"
                            className="w-full flex items-center justify-center py-2 rounded-lg text-white/40 hover:text-red-400 hover:bg-white/10 transition-colors disabled:opacity-40"
                        >
                            <LogOut className="w-4 h-4" />
                        </button>
                    )}
                </div>
            </aside>

            {/* ── Page content ────────────────────────────────────── */}
            <main className="relative flex-1 flex flex-col min-w-0 overflow-hidden">
                {status === "offline" && (
                    <div className="pointer-events-none absolute right-28 top-5 z-10 rounded-full border border-amber-200 bg-amber-50/90 px-2 py-1 text-xs font-semibold text-amber-700 shadow-sm backdrop-blur-sm">
                        offline
                    </div>
                )}
                {user?.mfa_warning && (
                    <div className="flex-shrink-0 flex items-center gap-3 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-amber-800 text-xs">
                        <ShieldAlert className="w-4 h-4 text-amber-600 flex-shrink-0" />
                        <span className="flex-1">
                            <strong>Security recommendation:</strong> Enable multi-factor authentication (MFA) for maximum account protection.
                        </span>
                        <button
                            onClick={() => navigate("/settings/security")}
                            className="font-semibold text-amber-700 hover:text-amber-900 underline underline-offset-2 whitespace-nowrap"
                        >
                            Set up now
                        </button>
                    </div>
                )}
                {children}
            </main>
        </div>
    );
}
