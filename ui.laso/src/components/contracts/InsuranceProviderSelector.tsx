/**
 * InsuranceProviderSelector.tsx
 * =============================
 * Smart insurance provider selector with:
 * - Search and filter insurance providers
 * - Create new insurance provider inline
 * - Display provider details (code, logo, settings)
 *
 * Used in contract creation/editing forms.
 */

import { useEffect, useMemo, useState, useCallback } from "react";
import { AlertCircle, ChevronDown, Loader2, Plus, X } from "lucide-react";
import { insuranceProvidersApi, type InsuranceProviderSearchItem, type InsuranceProviderCreate } from "@/api/insuranceProviders";
import { parseApiError } from "@/api/client";

interface InsuranceProviderSelectorProps {
    value: string | null;
    onChange: (id: string | null) => void;
    onCreated?: (id: string) => void;
    error?: string;
}

const inputCls = "w-full h-10 px-3 rounded-lg border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors";

export function InsuranceProviderSelector({
    value,
    onChange,
    onCreated,
    error,
}: InsuranceProviderSelectorProps) {
    const [providers, setProviders] = useState<InsuranceProviderSearchItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [showDropdown, setShowDropdown] = useState(false);
    const [showCreateForm, setShowCreateForm] = useState(false);
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);

    // Create form state
    const [newProviderData, setNewProviderData] = useState<InsuranceProviderCreate>({
        name: "",
        code: "",
        email: "",
        phone: "",
        is_active: true,
        billing_cycle: "monthly",
        payment_terms: "NET30",
    });

    // Load insurance providers
    const loadProviders = useCallback(async () => {
        setLoading(true);
        try {
            const result = await insuranceProvidersApi.search(searchQuery, true);
            setProviders(result);
        } catch (err) {
            console.error("Failed to load insurance providers:", err);
        } finally {
            setLoading(false);
        }
    }, [searchQuery]);

    useEffect(() => {
        const timer = setTimeout(() => void loadProviders(), 300);
        return () => clearTimeout(timer);
    }, [loadProviders]);

    // Get selected provider
    const selectedProvider = useMemo(
        () => providers.find((p) => p.id === value) ?? null,
        [providers, value]
    );

    // Handle creating new provider
    const handleCreateProvider = async () => {
        setCreateError(null);

        if (!newProviderData.name?.trim()) {
            setCreateError("Provider name is required");
            return;
        }

        if (!newProviderData.code?.trim()) {
            setCreateError("Provider code is required");
            return;
        }

        setCreating(true);
        try {
            const created = await insuranceProvidersApi.create(newProviderData);
            setProviders((prev) => [
                {
                    id: created.id,
                    name: created.name,
                    code: created.code,
                    logo_url: created.logo_url,
                    is_active: created.is_active,
                },
                ...prev,
            ]);
            onChange(created.id);
            setShowCreateForm(false);
            setShowDropdown(false);
            setNewProviderData({
                name: "",
                code: "",
                email: "",
                phone: "",
                is_active: true,
                billing_cycle: "monthly",
                payment_terms: "NET30",
            });
            onCreated?.(created.id);
        } catch (err) {
            setCreateError(parseApiError(err));
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="space-y-2">
            {/* Selected provider or dropdown trigger */}
            <div className="relative">
                <button
                    type="button"
                    onClick={() => setShowDropdown(!showDropdown)}
                    className={`${inputCls} flex items-center justify-between cursor-pointer ${error ? "border-red-300 bg-red-50/30" : ""}`}
                >
                    <span className="text-left">
                        {selectedProvider ? (
                            <div className="flex items-center gap-2">
                                {selectedProvider.logo_url && (
                                    <img src={selectedProvider.logo_url} alt={selectedProvider.name} className="w-5 h-5 rounded" />
                                )}
                                <div className="text-left">
                                    <p className="font-semibold text-ink">{selectedProvider.name}</p>
                                    <p className="text-xs text-ink-muted">{selectedProvider.code}</p>
                                </div>
                            </div>
                        ) : (
                            <span className="text-slate-400">Select insurance provider...</span>
                        )}
                    </span>
                    <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${showDropdown ? "rotate-180" : ""}`} />
                </button>

                {/* Dropdown */}
                {showDropdown && (
                    <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-lg z-50">
                        {/* Search box */}
                        <div className="p-2 border-b border-slate-100">
                            <input
                                type="text"
                                placeholder="Search providers..."
                                value={searchQuery}
                                onChange={(e) => setSearchQuery(e.target.value)}
                                className={inputCls}
                                autoFocus
                            />
                        </div>

                        {/* Loading state */}
                        {loading ? (
                            <div className="p-3 text-center text-slate-500">
                                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                                Loading providers...
                            </div>
                        ) : providers.length > 0 ? (
                            <div className="max-h-64 overflow-y-auto">
                                {providers.map((provider) => (
                                    <button
                                        key={provider.id}
                                        type="button"
                                        onClick={() => {
                                            onChange(provider.id);
                                            setShowDropdown(false);
                                        }}
                                        className={`w-full px-3 py-2.5 text-left text-sm hover:bg-brand-50 flex items-center gap-2 border-b border-slate-50 last:border-b-0 ${
                                            value === provider.id ? "bg-brand-50 border-l-2 border-l-brand-500" : ""
                                        }`}
                                    >
                                        {provider.logo_url && (
                                            <img src={provider.logo_url} alt={provider.name} className="w-4 h-4 rounded" />
                                        )}
                                        <div className="flex-1 min-w-0">
                                            <p className="font-semibold text-ink truncate">{provider.name}</p>
                                            <p className="text-xs text-slate-500">{provider.code}</p>
                                        </div>
                                        {!provider.is_active && (
                                            <span className="text-xs text-slate-400 font-semibold">Inactive</span>
                                        )}
                                    </button>
                                ))}
                            </div>
                        ) : (
                            <div className="p-3 text-center text-slate-500 text-sm">
                                No insurance providers found
                            </div>
                        )}

                        {/* Create new button */}
                        <button
                            type="button"
                            onClick={() => setShowCreateForm(!showCreateForm)}
                            className="w-full px-3 py-2.5 text-sm font-semibold text-brand-600 hover:bg-brand-50 border-t border-slate-100 flex items-center justify-center gap-1.5"
                        >
                            <Plus className="w-4 h-4" />
                            {showCreateForm ? "Cancel" : "Create New Provider"}
                        </button>

                        {/* Create form */}
                        {showCreateForm && (
                            <div className="space-y-2.5 p-3 border-t border-slate-100 bg-brand-50/30">
                                <div className="grid grid-cols-2 gap-2">
                                    <div>
                                        <label className="text-xs font-semibold text-ink-muted mb-1 block">
                                            Provider Name <span className="text-red-500">*</span>
                                        </label>
                                        <input
                                            type="text"
                                            placeholder="e.g., NHIS Ghana"
                                            value={newProviderData.name}
                                            onChange={(e) =>
                                                setNewProviderData((prev) => ({
                                                    ...prev,
                                                    name: e.target.value,
                                                }))
                                            }
                                            className={inputCls}
                                        />
                                    </div>
                                    <div>
                                        <label className="text-xs font-semibold text-ink-muted mb-1 block">
                                            Code <span className="text-red-500">*</span>
                                        </label>
                                        <input
                                            type="text"
                                            placeholder="e.g., NHIS_GH"
                                            value={newProviderData.code}
                                            onChange={(e) =>
                                                setNewProviderData((prev) => ({
                                                    ...prev,
                                                    code: e.target.value.toUpperCase(),
                                                }))
                                            }
                                            className={inputCls}
                                        />
                                    </div>
                                </div>

                                <div className="grid grid-cols-2 gap-2">
                                    <div>
                                        <label className="text-xs font-semibold text-ink-muted mb-1 block">Email</label>
                                        <input
                                            type="email"
                                            placeholder="contact@provider.com"
                                            value={newProviderData.email}
                                            onChange={(e) =>
                                                setNewProviderData((prev) => ({
                                                    ...prev,
                                                    email: e.target.value,
                                                }))
                                            }
                                            className={inputCls}
                                        />
                                    </div>
                                    <div>
                                        <label className="text-xs font-semibold text-ink-muted mb-1 block">Phone</label>
                                        <input
                                            type="tel"
                                            placeholder="+233-302-670670"
                                            value={newProviderData.phone}
                                            onChange={(e) =>
                                                setNewProviderData((prev) => ({
                                                    ...prev,
                                                    phone: e.target.value,
                                                }))
                                            }
                                            className={inputCls}
                                        />
                                    </div>
                                </div>

                                {createError && (
                                    <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1.5 flex items-center gap-1">
                                        <AlertCircle className="w-3 h-3 flex-shrink-0" />
                                        {createError}
                                    </p>
                                )}

                                <button
                                    type="button"
                                    onClick={handleCreateProvider}
                                    disabled={creating}
                                    className="w-full h-9 rounded-lg bg-brand-600 text-white text-sm font-semibold hover:bg-brand-700 disabled:opacity-60 flex items-center justify-center gap-1.5"
                                >
                                    {creating ? (
                                        <>
                                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                            Creating...
                                        </>
                                    ) : (
                                        <>
                                            <Plus className="w-3.5 h-3.5" />
                                            Create Provider
                                        </>
                                    )}
                                </button>
                            </div>
                        )}
                    </div>
                )}

                {/* Clear selection button */}
                {value && (
                    <button
                        type="button"
                        onClick={() => onChange(null)}
                        className="absolute right-10 top-2.5 p-1 text-slate-400 hover:text-red-500"
                        title="Clear selection"
                    >
                        <X className="w-4 h-4" />
                    </button>
                )}
            </div>

            {/* Error message */}
            {error && (
                <p className="text-xs text-red-600 flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" />
                    {error}
                </p>
            )}

            {/* Help text */}
            <p className="text-xs text-slate-500">
                Select an insurance provider or create a new one. New providers will be marked active and available for other contracts.
            </p>
        </div>
    );
}
