/**
 * =================
 * Customer management page.
 *
 * Features:
 *  - Paginated list with search by name/phone/email/member_id
 *  - Type filter (walk_in / registered / insurance / corporate)
 *  - Loyalty tier badges (bronze / silver / gold / platinum)
 *  - Customer detail drawer (purchase stats, insurance, contract)
 *  - Create / Edit / Deactivate
 *  - Role-gated: cashiers can create walk_in; managers+ can create all types
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
    Plus, RefreshCw, Search, X,
    User, Shield, Building2, Users,
    Star, ChevronLeft, ChevronRight,
    AlertTriangle, Edit2, ToggleLeft, ToggleRight,
    ShoppingBag, Phone, Mail, Award,
} from "lucide-react";
import { customersApi, type CustomerWithDetails } from "@/api/customers";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";
import { useDebounce } from "@/hooks/useDebounce";
import { CustomerForm } from "@/components/customers/CustomerForm";
import type { Customer } from "@/types";

// ── Helpers ───────────────────────────────────────────────────────────────────

const TIER_CONFIG: Record<string, { label: string; cls: string }> = {
    bronze: { label: "Bronze", cls: "bg-amber-100 text-amber-800" },
    silver: { label: "Silver", cls: "bg-slate-200 text-slate-700" },
    gold: { label: "Gold", cls: "bg-yellow-100 text-yellow-700" },
    platinum: { label: "Platinum", cls: "bg-purple-100 text-purple-700" },
};

const TYPE_CONFIG: Record<string, { label: string; cls: string; icon: React.ElementType }> = {
    walk_in: { label: "Walk-in", cls: "bg-slate-100 text-slate-600", icon: User },
    registered: { label: "Registered", cls: "bg-green-50 text-green-700", icon: Users },
    insurance: { label: "Insurance", cls: "bg-blue-50 text-blue-700", icon: Shield },
    corporate: { label: "Corporate", cls: "bg-purple-50 text-purple-700", icon: Building2 },
};

// ── Component ─────────────────────────────────────────────────────────────────

export default function CustomersPage() {
    const { user } = useAuthStore();
    const canManage = !!user?.role && ["super_admin", "admin", "manager", "pharmacist"].includes(user.role);

    // ── List state ─────────────────────────────────────────────────────────────
    const [customers, setCustomers] = useState<CustomerWithDetails[]>([]);
    const [total, setTotal] = useState(0);
    const [totalPages, setTotalPages] = useState(1);
    const [page, setPage] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [searchInput, setSearchInput] = useState("");
    const debouncedSearch = useDebounce(searchInput, 300);
    const [filterType, setFilterType] = useState("");
    const [filterTier, setFilterTier] = useState("");

    // ── Modal / drawer state ───────────────────────────────────────────────────
    const [showCreate, setShowCreate] = useState(false);
    const [editingCustomer, setEditingCustomer] = useState<CustomerWithDetails | null>(null);
    const [detailCustomer, setDetailCustomer] = useState<CustomerWithDetails | null>(null);
    const [togglingId, setTogglingId] = useState<string | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);

    const abortRef = useRef<AbortController | null>(null);

    const fetchCustomers = useCallback(async () => {
        abortRef.current?.abort();
        const ctrl = new AbortController();
        abortRef.current = ctrl;

        setIsLoading(true);
        setError(null);

        try {
            const res = await customersApi.list(
                {
                    page,
                    page_size: 25,
                    search: debouncedSearch || undefined,
                    customer_type: filterType || undefined,
                    loyalty_tier: filterTier || undefined,
                    sort_by: "created_at",
                    sort_order: "desc",
                },
                ctrl.signal
            );
            if (!ctrl.signal.aborted) {
                setCustomers(res.customers as CustomerWithDetails[]);
                setTotal(res.total);
                setTotalPages(res.total_pages);
            }
        } catch (err: unknown) {
            if (err instanceof Error && err.name === "AbortError") return;
            if (!ctrl.signal.aborted) setError(parseApiError(err));
        } finally {
            if (!ctrl.signal.aborted) setIsLoading(false);
        }
    }, [page, debouncedSearch, filterType, filterTier]);

    useEffect(() => { fetchCustomers(); return () => abortRef.current?.abort(); }, [fetchCustomers]);

    // ── Toggle active ──────────────────────────────────────────────────────────
    const handleToggleActive = async (customer: CustomerWithDetails) => {
        setTogglingId(customer.id);
        // Optimistically flip the flag in the list immediately
        setCustomers((prev) => prev.map((c) => c.id === customer.id ? { ...c, is_active: !c.is_active } : c));
        try {
            await customersApi.update(customer.id, { is_active: !customer.is_active });
        } catch (err) {
            // Revert on failure
            setCustomers((prev) => prev.map((c) => c.id === customer.id ? { ...c, is_active: customer.is_active } : c));
            setError(parseApiError(err));
        } finally { setTogglingId(null); }
    };

    const handleSaved = (saved: CustomerWithDetails) => {
        setCustomers((prev) => {
            const idx = prev.findIndex((c) => c.id === saved.id);
            if (idx >= 0) { const next = [...prev]; next[idx] = saved; return next; }
            return [saved, ...prev];
        });
        setTotal((t) => showCreate ? t + 1 : t);
        setShowCreate(false);
        setEditingCustomer(null);
    };

    const openDetail = async (c: CustomerWithDetails) => {
        setDetailCustomer(c); // show drawer immediately with list data
        setDetailLoading(true);
        try {
            const full = await customersApi.getById(c.id);
            setDetailCustomer(full);
        } catch {
            // Non-blocking — drawer still shows with list data
        } finally {
            setDetailLoading(false);
        }
    };

    const fullName = (c: Customer) => {
        if (c.first_name && c.last_name) return `${c.first_name} ${c.last_name}`;
        return c.first_name ?? c.last_name ?? "Walk-in Customer";
    };

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex-shrink-0">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="font-display text-2xl font-bold text-ink">Customers</h1>
                        <p className="text-sm text-ink-muted mt-0.5">{total} customer{total !== 1 ? "s" : ""} registered</p>
                    </div>
                    <div className="flex items-center gap-2">
                        <button onClick={fetchCustomers} className="w-9 h-9 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors" title="Refresh">
                            <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} />
                        </button>
                        <button onClick={() => setShowCreate(true)} className="flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors">
                            <Plus className="w-4 h-4" />Register Customer
                        </button>
                    </div>
                </div>
            </div>

            {/* Filters */}
            <div className="px-6 py-3 bg-white border-b border-slate-100 flex items-center gap-3 flex-wrap flex-shrink-0">
                <div className="relative flex-1 min-w-48">
                    <Search className="absolute left-3 top-2.5 w-4 h-4 text-ink-muted" />
                    <input
                        value={searchInput}
                        onChange={(e) => { setSearchInput(e.target.value); setPage(1); }}
                        placeholder="Search name, phone, email, member ID…"
                        className="w-full pl-9 pr-3 py-2 text-sm rounded-xl border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    />
                </div>
                <select value={filterType} onChange={(e) => { setFilterType(e.target.value); setPage(1); }} className="text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none">
                    <option value="">All types</option>
                    <option value="walk_in">Walk-in</option>
                    <option value="registered">Registered</option>
                    <option value="insurance">Insurance</option>
                    <option value="corporate">Corporate</option>
                </select>
                <select value={filterTier} onChange={(e) => { setFilterTier(e.target.value); setPage(1); }} className="text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white focus:outline-none">
                    <option value="">All tiers</option>
                    <option value="bronze">Bronze</option>
                    <option value="silver">Silver</option>
                    <option value="gold">Gold</option>
                    <option value="platinum">Platinum</option>
                </select>
                {(searchInput || filterType || filterTier) && (
                    <button onClick={() => { setSearchInput(""); setFilterType(""); setFilterTier(""); setPage(1); }}
                        className="flex items-center gap-1 text-xs text-ink-muted hover:text-red-500 transition-colors">
                        <X className="w-3.5 h-3.5" />Clear
                    </button>
                )}
            </div>

            {/* Error */}
            {error && (
                <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-start flex-shrink-0">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-600 flex-1">{error}</p>
                    <button onClick={() => setError(null)}><X className="w-4 h-4 text-ink-muted" /></button>
                </div>
            )}

            {/* Table */}
            <div className="flex-1 overflow-auto px-6 py-4">
                {isLoading ? (
                    <div className="flex items-center justify-center h-48">
                        <div className="w-6 h-6 border-2 border-brand-500 border-t-transparent rounded-full animate-spin" />
                    </div>
                ) : customers.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 gap-3 text-ink-muted">
                        <Users className="w-10 h-10 opacity-25" />
                        <p className="text-sm font-medium">No customers found</p>
                        <button onClick={() => setShowCreate(true)} className="text-sm text-brand-600 hover:underline">Register your first customer</button>
                    </div>
                ) : (
                    <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="bg-slate-50 border-b border-slate-200">
                                    <th className="text-left px-5 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Customer</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Type</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Contact</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Loyalty</th>

                                    <th className="text-center px-4 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Status</th>
                                    <th className="text-right px-5 py-3 text-xs font-semibold text-ink-muted uppercase tracking-wide">Actions</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100">
                                {customers.map((c) => {
                                    const typeCfg = TYPE_CONFIG[c.customer_type] ?? TYPE_CONFIG.walk_in;
                                    const TypeIcon = typeCfg.icon;
                                    const tierCfg = TIER_CONFIG[c.loyalty_tier ?? "bronze"];
                                    const isToggling = togglingId === c.id;

                                    return (
                                        <motion.tr
                                            key={c.id}
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            className={`hover:bg-slate-50/70 transition-colors group cursor-pointer ${!c.is_active ? "opacity-60" : ""}`}
                                            onClick={() => openDetail(c)}
                                        >
                                            {/* Name */}
                                            <td className="px-5 py-3">
                                                <div className="flex items-center gap-3">
                                                    <div className="w-8 h-8 rounded-full bg-brand-100 flex items-center justify-center flex-shrink-0">
                                                        <span className="text-xs font-bold text-brand-700">
                                                            {(c.first_name?.[0] ?? c.last_name?.[0] ?? "?").toUpperCase()}
                                                        </span>
                                                    </div>
                                                    <div>
                                                        <p className="font-semibold text-ink">{fullName(c)}</p>
                                                        {c.insurance_member_id && (
                                                            <p className="text-xs font-mono text-ink-muted">ID: {c.insurance_member_id}</p>
                                                        )}
                                                    </div>
                                                </div>
                                            </td>
                                            {/* Type */}
                                            <td className="px-4 py-3">
                                                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${typeCfg.cls}`}>
                                                    <TypeIcon className="w-3 h-3" />
                                                    {typeCfg.label}
                                                </span>
                                            </td>
                                            {/* Contact */}
                                            <td className="px-4 py-3">
                                                <div className="text-xs text-ink-secondary space-y-0.5">
                                                    {c.phone && <p className="flex items-center gap-1"><Phone className="w-3 h-3" />{c.phone}</p>}
                                                    {c.email && <p className="flex items-center gap-1 truncate max-w-36"><Mail className="w-3 h-3" />{c.email}</p>}
                                                    {!c.phone && !c.email && <span className="text-ink-muted">—</span>}
                                                </div>
                                            </td>
                                            {/* Loyalty */}
                                            <td className="px-4 py-3">
                                                <div className="space-y-1">
                                                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${tierCfg?.cls ?? ""}`}>
                                                        {tierCfg?.label ?? c.loyalty_tier}
                                                    </span>
                                                    <p className="text-xs text-ink-muted flex items-center gap-1">
                                                        <Star className="w-3 h-3 text-amber-400" />
                                                        {c.loyalty_points?.toLocaleString() ?? 0} pts
                                                    </p>
                                                </div>
                                            </td>

                                            {/* Status */}
                                            <td className="px-4 py-3 text-center">
                                                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${c.is_active ? "bg-green-50 text-green-700" : "bg-slate-100 text-slate-500"}`}>
                                                    {c.is_active ? "Active" : "Inactive"}
                                                </span>
                                            </td>
                                            {/* Actions */}
                                            <td className="px-5 py-3" onClick={(e) => e.stopPropagation()}>
                                                <div className="flex items-center justify-end gap-1">
                                                    <button
                                                        onClick={() => setEditingCustomer(c)}
                                                        title="Edit"
                                                        className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                                                    >
                                                        <Edit2 className="w-3.5 h-3.5" />
                                                    </button>
                                                    {canManage && (
                                                        <button
                                                            onClick={() => handleToggleActive(c)}
                                                            disabled={isToggling}
                                                            title={c.is_active ? "Deactivate" : "Activate"}
                                                            className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors disabled:opacity-50"
                                                        >
                                                            {isToggling
                                                                ? <div className="w-3.5 h-3.5 border border-slate-400 border-t-transparent rounded-full animate-spin" />
                                                                : c.is_active
                                                                    ? <ToggleRight className="w-4 h-4 text-green-600" />
                                                                    : <ToggleLeft className="w-4 h-4 text-slate-400" />
                                                            }
                                                        </button>
                                                    )}
                                                </div>
                                            </td>
                                        </motion.tr>
                                    );
                                })}
                            </tbody>
                        </table>

                        {/* Pagination */}
                        {totalPages > 1 && (
                            <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-slate-50">
                                <p className="text-xs text-ink-muted">
                                    {((page - 1) * 25) + 1}–{Math.min(page * 25, total)} of {total}
                                </p>
                                <div className="flex items-center gap-2">
                                    <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}
                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted hover:text-ink hover:bg-white disabled:opacity-40 transition-colors">
                                        <ChevronLeft className="w-4 h-4" />
                                    </button>
                                    <span className="text-xs text-ink font-medium">Page {page} of {totalPages}</span>
                                    <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                                        className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 text-ink-muted hover:text-ink hover:bg-white disabled:opacity-40 transition-colors">
                                        <ChevronRight className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Detail drawer */}
            <AnimatePresence>
                {detailCustomer && (
                    <>
                        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
                            className="fixed inset-0 z-40 bg-black/20" onClick={() => setDetailCustomer(null)} />
                        <motion.div
                            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
                            transition={{ type: "spring", damping: 30, stiffness: 300 }}
                            className="fixed right-0 top-0 bottom-0 z-50 w-80 bg-white shadow-2xl flex flex-col"
                        >
                            <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
                                <h3 className="font-display font-bold text-ink">{fullName(detailCustomer)}</h3>
                                <button onClick={() => setDetailCustomer(null)} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100">
                                    <X className="w-4 h-4" />
                                </button>
                            </div>
                            <div className="flex-1 overflow-y-auto p-5 space-y-4">
                                {/* Type + tier */}
                                <div className="flex gap-2">
                                    {(() => {
                                        const cfg = TYPE_CONFIG[detailCustomer.customer_type]; const Icon = cfg.icon; return (
                                            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-semibold ${cfg.cls}`}>
                                                <Icon className="w-3 h-3" />{cfg.label}
                                            </span>
                                        );
                                    })()}
                                    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-semibold ${TIER_CONFIG[detailCustomer.loyalty_tier ?? "bronze"]?.cls}`}>
                                        <Award className="w-3 h-3" />{TIER_CONFIG[detailCustomer.loyalty_tier ?? "bronze"]?.label}
                                    </span>
                                </div>

                                {/* Contact */}
                                <div className="space-y-2">
                                    <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide">Contact</p>
                                    {detailCustomer.phone && <p className="text-sm text-ink flex items-center gap-2"><Phone className="w-3.5 h-3.5 text-ink-muted" />{detailCustomer.phone}</p>}
                                    {detailCustomer.email && <p className="text-sm text-ink flex items-center gap-2"><Mail className="w-3.5 h-3.5 text-ink-muted" />{detailCustomer.email}</p>}
                                </div>

                                {/* Loyalty */}
                                <div className="rounded-xl bg-amber-50 border border-amber-100 p-3">
                                    <p className="text-xs font-semibold text-amber-700 mb-1.5 flex items-center gap-1"><Star className="w-3.5 h-3.5" />Loyalty</p>
                                    <p className="text-2xl font-bold text-amber-700">{detailCustomer.loyalty_points?.toLocaleString() ?? 0}</p>
                                    <p className="text-xs text-amber-600">points · {detailCustomer.loyalty_tier} tier</p>
                                </div>

                                {/* Purchase stats */}
                                <div className="rounded-xl bg-brand-50 border border-brand-100 p-3">
                                    <p className="text-xs font-semibold text-brand-700 mb-2 flex items-center gap-1">
                                        <ShoppingBag className="w-3.5 h-3.5" />Purchase History
                                        {detailLoading && <RefreshCw className="w-3 h-3 ml-auto animate-spin text-brand-400" />}
                                    </p>
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <p className="text-xl font-bold text-brand-700">{detailCustomer.total_purchases ?? 0}</p>
                                            <p className="text-xs text-brand-600">total purchases</p>
                                        </div>
                                        <div>
                                            <p className="text-xl font-bold text-brand-700">₵{(detailCustomer.total_spent ?? 0).toFixed(2)}</p>
                                            <p className="text-xs text-brand-600">total spent</p>
                                        </div>
                                    </div>
                                    {detailCustomer.last_purchase_date && (
                                        <p className="text-xs text-brand-600 mt-2">
                                            Last purchase: {new Date(detailCustomer.last_purchase_date).toLocaleDateString()}
                                        </p>
                                    )}
                                </div>

                                {/* Insurance */}
                                {detailCustomer.insurance_provider_id && (
                                    <div className="rounded-xl bg-blue-50 border border-blue-100 p-3">
                                        <p className="text-xs font-semibold text-blue-700 mb-1.5 flex items-center gap-1"><Shield className="w-3.5 h-3.5" />Insurance</p>
                                        {detailCustomer.insurance_provider_name && <p className="text-sm font-semibold text-blue-800">{detailCustomer.insurance_provider_name}</p>}
                                        <p className="text-xs text-blue-600 font-mono">ID: {detailCustomer.insurance_member_id}</p>
                                    </div>
                                )}

                                {/* Preferred contract */}
                                {detailCustomer.preferred_contract_name && (
                                    <div className="rounded-xl bg-purple-50 border border-purple-100 p-3">
                                        <p className="text-xs font-semibold text-purple-700 mb-1 flex items-center gap-1"><Building2 className="w-3.5 h-3.5" />Preferred Contract</p>
                                        <p className="text-sm font-semibold text-purple-800">{detailCustomer.preferred_contract_name}</p>
                                        {detailCustomer.preferred_contract_discount != null && (
                                            <p className="text-xs text-purple-600">{detailCustomer.preferred_contract_discount}% discount</p>
                                        )}
                                    </div>
                                )}
                            </div>
                            <div className="px-5 py-3 border-t border-slate-100">
                                <button
                                    onClick={() => { setEditingCustomer(detailCustomer); setDetailCustomer(null); }}
                                    className="w-full py-2.5 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors flex items-center justify-center gap-2"
                                >
                                    <Edit2 className="w-4 h-4" />Edit Customer
                                </button>
                            </div>
                        </motion.div>
                    </>
                )}
            </AnimatePresence>

            {/* Modals */}
            <AnimatePresence>
                {(showCreate || editingCustomer) && (
                    <CustomerForm
                        customer={editingCustomer ?? undefined}
                        onSuccess={handleSaved}
                        onCancel={() => { setShowCreate(false); setEditingCustomer(null); }}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}