/**
 * ─────────────────────────────────────────────────────────────
 * Admin-only settings hub. Currently exposes the Branches tab.
 * Structured for easy expansion: add Users, Billing, Org tabs
 * by dropping new <Tab> entries and lazy-loading their panels.
 *
 * Access: admin | super_admin only (enforced in RequireAuth +
 * the nav item's roles filter in AppShell).
 */

import { useState } from "react";
import { GitBranch, Building2, ChevronRight } from "lucide-react";
import { BranchesTab } from "@/components/settings/BranchesTab";
import { OrganizationTab } from "@/components/settings/OrganizationTab";

type TabId = "branches" | "organization";

interface Tab {
    id: TabId;
    label: string;
    icon: React.ElementType;
    description: string;
}

const TABS: Tab[] = [
    {
        id: "organization",
        icon: Building2,
        label: "Organisation",
        description: "Profile & settings",
    },
    {
        id: "branches",
        icon: GitBranch,
        label: "Branches",
        description: "Manage locations",
    },
];

export default function SettingsPage() {
    const [activeTab, setActiveTab] = useState<TabId>("organization");

    const active = TABS.find((t) => t.id === activeTab)!;

    return (
        <div className="flex flex-col h-full bg-surface">

            {/* Page header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex-shrink-0">
                <div className="flex items-center gap-2 text-xs text-slate-400 mb-1">
                    <Building2 className="w-3.5 h-3.5" />
                    <span>Organization</span>
                    <ChevronRight className="w-3 h-3" />
                    <span className="text-ink font-medium">{active.label}</span>
                </div>
                <h1 className="font-display text-2xl font-bold text-ink">Settings</h1>
                <p className="text-sm text-ink-muted mt-0.5">
                    Manage your organization's configuration
                </p>
            </div>

            <div className="flex flex-1 min-h-0 overflow-hidden">

                {/* Sidebar nav */}
                <nav className="w-52 flex-shrink-0 border-r border-slate-200 bg-white px-2 py-3 space-y-0.5 overflow-y-auto">
                    {TABS.map((tab) => {
                        const Icon = tab.icon;
                        const isActive = tab.id === activeTab;
                        return (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-left transition-colors ${isActive
                                        ? "bg-brand-50 text-brand-700"
                                        : "text-ink-secondary hover:bg-slate-50 hover:text-ink"
                                    }`}
                            >
                                <Icon className={`w-4 h-4 flex-shrink-0 ${isActive ? "text-brand-600" : "text-slate-400"}`} />
                                <div className="min-w-0">
                                    <p className={`text-sm font-semibold truncate ${isActive ? "text-brand-700" : ""}`}>
                                        {tab.label}
                                    </p>
                                    <p className="text-[10px] text-slate-400 truncate">{tab.description}</p>
                                </div>
                            </button>
                        );
                    })}
                </nav>

                {/* Tab content */}
                <div className="flex-1 min-h-0 overflow-hidden">
                    {activeTab === "organization" && <OrganizationTab />}
                    {activeTab === "branches" && <BranchesTab />}
                </div>
            </div>
        </div>
    );
}