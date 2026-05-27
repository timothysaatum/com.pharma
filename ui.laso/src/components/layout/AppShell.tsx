import { useState, useEffect } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
    Pill, ShoppingCart, Users, BarChart2,
    LogOut, Building2, Menu, X,
    Receipt, Settings, UserCog, Cog,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { branchApi } from "@/api/branches";
import { offlineCache } from "@/lib/storage";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { SyncIndicator } from "@/components/layout/SyncIndicator";

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
];

const ROLE_LABELS: Record<string, string> = {
    super_admin: "Super Admin",
    admin: "Admin",
    manager: "Manager",
    pharmacist: "Pharmacist",
    cashier: "Cashier",
    viewer: "Viewer",
};

export function AppShell({ children }: { children: React.ReactNode }) {
    const { user, logout, activeBranchId } = useAuthStore();
    const navigate = useNavigate();
    const [collapsed, setCollapsed] = useState(false);
    const [loggingOut, setLoggingOut] = useState(false);
    const [branchName, setBranchName] = useState<string | null | undefined>(undefined);

    useEffect(() => {
        if (!activeBranchId) {
            setBranchName(undefined);
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

    const handleLogout = async () => {
        setLoggingOut(true);
        await logout();
        navigate("/login", { replace: true });
    };

    const visibleNav = NAV_ITEMS.filter(
        (item) => !item.roles || (user?.role && item.roles.includes(user.role))
    );

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
                            alt="Laso logo"
                            className="w-full h-full rounded-lg object-contain"
                        />
                    </div>
                    {!collapsed && (
                        <span className="font-display font-bold text-sm flex-1 truncate">
                            Laso
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

                {/* Branch pill */}
                {!collapsed && activeBranchId && (
                    <div className="mx-3 mt-3 px-2.5 py-1.5 rounded-lg bg-white/5 border border-white/10 flex items-center gap-2">
                        <Building2 className="w-3 h-3 text-white/40 flex-shrink-0" />
                        <span
                            className="text-xs text-white/60 truncate"
                            title={branchName || undefined}
                        >
                            {branchName === undefined ? "Loading…" : branchName ?? "Branch unavailable"}
                        </span>
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
                                    {ROLE_LABELS[user?.role ?? ""] ?? user?.role}
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
                {children}
            </main>
        </div>
    );
}
