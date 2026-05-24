/**
 * ─────────────────────────────────────────────────────────────
 * Two-section settings panel for the current organisation:
 *
 *  1. Profile  — name, phone, email, address
 *     → PATCH /organizations/{id}
 *
 *  2. Operational Settings — currency, timezone, thresholds, features
 *     → PATCH /organizations/{id}/settings
 *
 * Each section is a self-contained form with its own save button,
 * loading state, error banner, and success feedback. Stats bar
 * shows a live overview at the top for quick context.
 *
 * Access: admin | super_admin (enforced at route + server level).
 */

import { useState, useEffect, useCallback } from "react";
import {
    Building2, Phone, Mail, MapPin, RefreshCw,
    AlertTriangle, CheckCircle2, Save, Globe,
    DollarSign, Users, Pill, ShoppingBag,
    TrendingUp, AlertCircle,
} from "lucide-react";
import { useOrganization } from "@/hooks/useOrganization";
import type { OrganizationUpdate } from "@/types";
import type { OrganizationSettingsUpdate } from "@/api/organization";

// ─── Shared input styles ─────────────────────────────────────

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 " +
    "transition-colors placeholder:text-slate-400 disabled:bg-slate-50 disabled:text-slate-400";

const labelCls =
    "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

const sectionCls =
    "bg-white rounded-2xl border border-slate-200 overflow-hidden";

// ─── Stats bar ────────────────────────────────────────────────

function StatCard({
    icon: Icon,
    label,
    value,
    accent,
}: {
    icon: React.ElementType;
    label: string;
    value: number | string;
    accent: string;
}) {
    return (
        <div className="flex items-center gap-3 p-4">
            <div className={`w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 ${accent}`}>
                <Icon className="w-4 h-4" />
            </div>
            <div>
                <p className="text-lg font-bold text-ink leading-tight">{value}</p>
                <p className="text-xs text-slate-400 leading-tight">{label}</p>
            </div>
        </div>
    );
}

// ─── Feedback inline banner ───────────────────────────────────

function FeedbackBanner({ error, success, successMsg }: { error: string | null; success: boolean; successMsg: string }) {
    if (error) {
        return (
            <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                {error}
            </div>
        );
    }
    if (success) {
        return (
            <div className="flex items-center gap-2 p-3 rounded-xl bg-green-50 border border-green-100 text-xs text-green-700">
                <CheckCircle2 className="w-3.5 h-3.5" />
                {successMsg}
            </div>
        );
    }
    return null;
}

// ─── Save footer ──────────────────────────────────────────────

function SaveFooter({
    loading,
    isDirty,
    onSave,
    onReset,
}: {
    loading: boolean;
    isDirty: boolean;
    onSave: () => void;
    onReset: () => void;
}) {
    if (!isDirty && !loading) return null;
    return (
        <div className="flex items-center justify-end gap-3 px-5 py-3 border-t border-slate-100 bg-slate-50/60">
            <button
                onClick={onReset}
                type="button"
                disabled={loading}
                className="px-4 py-2 text-xs font-semibold text-ink-secondary border border-slate-200 rounded-xl hover:bg-white disabled:opacity-50 transition-colors"
            >
                Reset
            </button>
            <button
                onClick={onSave}
                disabled={loading || !isDirty}
                type="button"
                className="px-5 py-2 text-xs font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
            >
                {loading ? (
                    <>
                        <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                        </svg>
                        Saving…
                    </>
                ) : (
                    <>
                        <Save className="w-3.5 h-3.5" />
                        Save Changes
                    </>
                )}
            </button>
        </div>
    );
}

// ─── Profile section ──────────────────────────────────────────

interface ProfileSectionProps {
    org: ReturnType<typeof useOrganization>["org"];
    orgMutation: ReturnType<typeof useOrganization>["orgMutation"];
    updateOrg: ReturnType<typeof useOrganization>["updateOrg"];
}

function ProfileSection({ org, orgMutation, updateOrg }: ProfileSectionProps) {

    const [name, setName] = useState("");
    const [phone, setPhone] = useState("");
    const [email, setEmail] = useState("");
    const [street, setStreet] = useState("");
    const [city, setCity] = useState("");
    const [state, setState] = useState("");
    const [country, setCountry] = useState("");

    // Sync from loaded org
    useEffect(() => {
        if (!org) return;
        setName(org.name ?? "");
        setPhone(org.phone ?? "");
        setEmail(org.email ?? "");
        setStreet(org.address?.street ?? "");
        setCity(org.address?.city ?? "");
        setState(org.address?.state ?? "");
        setCountry(org.address?.country ?? "Ghana");
    }, [org?.id]);

    const reset = useCallback(() => {
        if (!org) return;
        setName(org.name ?? "");
        setPhone(org.phone ?? "");
        setEmail(org.email ?? "");
        setStreet(org.address?.street ?? "");
        setCity(org.address?.city ?? "");
        setState(org.address?.state ?? "");
        setCountry(org.address?.country ?? "Ghana");
    }, [org]);

    const isDirty = org != null && (
        name !== (org.name ?? "") ||
        phone !== (org.phone ?? "") ||
        email !== (org.email ?? "") ||
        street !== (org.address?.street ?? "") ||
        city !== (org.address?.city ?? "") ||
        state !== (org.address?.state ?? "") ||
        country !== (org.address?.country ?? "Ghana")
    );

    const handleSave = async () => {
        if (!org) return;
        const hasAddress = street || city || state || country;
        const data: OrganizationUpdate = {
            name: name || undefined,
            phone: phone || undefined,
            email: email || undefined,
            address: hasAddress ? {
                street: street || undefined,
                city: city || undefined,
                state: state || undefined,
                country: country || "Ghana",
            } : undefined,
        };
        await updateOrg(data);
    };

    if (!org) return null;

    return (
        <div className={sectionCls}>
            <div className="px-5 py-3.5 border-b border-slate-100">
                <h3 className="text-sm font-bold text-ink">Organisation Profile</h3>
                <p className="text-xs text-slate-400 mt-0.5">Name, contact details and address</p>
            </div>

            <div className="px-5 py-4 space-y-4">
                <FeedbackBanner
                    error={orgMutation.error}
                    success={orgMutation.success}
                    successMsg="Organisation profile saved successfully"
                />

                {/* Name + Type (type is read-only after creation) */}
                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <label className={labelCls}>
                            Organisation Name <span className="text-red-400">*</span>
                        </label>
                        <div className="relative">
                            <Building2 className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="Organisation name"
                                className={`${inputCls} pl-9`}
                            />
                        </div>
                    </div>
                    <div>
                        <label className={labelCls}>Type</label>
                        <input
                            value={org.type}
                            disabled
                            className={inputCls}
                            title="Organisation type cannot be changed after creation"
                        />
                    </div>
                </div>

                {/* Phone + Email */}
                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <label className={labelCls}>Phone</label>
                        <div className="relative">
                            <Phone className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                type="tel"
                                value={phone}
                                onChange={(e) => setPhone(e.target.value)}
                                placeholder="+233 XX XXX XXXX"
                                className={`${inputCls} pl-9`}
                            />
                        </div>
                    </div>
                    <div>
                        <label className={labelCls}>Email</label>
                        <div className="relative">
                            <Mail className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                placeholder="org@example.com"
                                className={`${inputCls} pl-9`}
                            />
                        </div>
                    </div>
                </div>

                {/* Address */}
                <div>
                    <label className={labelCls}>
                        <MapPin className="inline w-3 h-3 mr-1 mb-0.5" />
                        Address
                    </label>
                    <div className="space-y-2">
                        <input
                            value={street}
                            onChange={(e) => setStreet(e.target.value)}
                            placeholder="Street address"
                            className={inputCls}
                        />
                        <div className="grid grid-cols-3 gap-2">
                            <input value={city} onChange={(e) => setCity(e.target.value)} placeholder="City" className={inputCls} />
                            <input value={state} onChange={(e) => setState(e.target.value)} placeholder="Region" className={inputCls} />
                            <input value={country} onChange={(e) => setCountry(e.target.value)} placeholder="Country" className={inputCls} />
                        </div>
                    </div>
                </div>

                {/* Read-only fields */}
                <div className="grid grid-cols-2 gap-4 pt-1">
                    <div>
                        <label className={labelCls}>Subscription Tier</label>
                        <input value={org.subscription_tier} disabled className={inputCls} />
                    </div>
                    <div>
                        <label className={labelCls}>Subscription Expires</label>
                        <input
                            value={
                                org.subscription_expires_at
                                    ? new Date(org.subscription_expires_at).toLocaleDateString("en-GH", { dateStyle: "medium" })
                                    : "—"
                            }
                            disabled
                            className={inputCls}
                        />
                    </div>
                </div>
            </div>

            <SaveFooter loading={orgMutation.loading} isDirty={isDirty} onSave={handleSave} onReset={reset} />
        </div>
    );
}

// ─── Operational settings section ────────────────────────────

const CURRENCIES = ["GHS", "USD", "EUR", "GBP", "NGN", "KES", "ZAR"];
const TIMEZONES = [
    "Africa/Accra", "Africa/Lagos", "Africa/Nairobi", "Africa/Cairo",
    "Africa/Johannesburg", "Europe/London", "America/New_York", "UTC",
];

function ToggleRow({
    label,
    description,
    value,
    onChange,
}: {
    label: string;
    description: string;
    value: boolean;
    onChange: (v: boolean) => void;
}) {
    return (
        <div className={`flex items-center justify-between py-3 px-4 rounded-xl border transition-colors ${value ? "bg-brand-50/40 border-brand-200" : "bg-slate-50 border-slate-200"
            }`}>
            <div>
                <p className="text-sm font-semibold text-ink">{label}</p>
                <p className="text-xs text-slate-400 mt-0.5">{description}</p>
            </div>
            <button
                type="button"
                onClick={() => onChange(!value)}
                className="flex-shrink-0 ml-4"
            >
                <span className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${value ? "bg-brand-600" : "bg-slate-200"}`}>
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${value ? "translate-x-6" : "translate-x-1"}`} />
                </span>
            </button>
        </div>
    );
}

interface OperationalSettingsSectionProps {
    org: ReturnType<typeof useOrganization>["org"];
    settingsMutation: ReturnType<typeof useOrganization>["settingsMutation"];
    updateSettings: ReturnType<typeof useOrganization>["updateSettings"];
}

function OperationalSettingsSection({ org, settingsMutation, updateSettings }: OperationalSettingsSectionProps) {

    const settings = (org?.settings ?? {}) as Record<string, unknown>;

    const [currency, setCurrency] = useState("GHS");
    const [timezone, setTimezone] = useState("Africa/Accra");
    const [lowStockThreshold, setLowStockThreshold] = useState(10);
    const [taxInclusive, setTaxInclusive] = useState(false);
    const [enableLoyalty, setEnableLoyalty] = useState(false);
    const [enablePrescriptions, setEnablePrescriptions] = useState(false);
    const [enableBatchTracking, setEnableBatchTracking] = useState(true);
    const [receiptFooter, setReceiptFooter] = useState("");

    useEffect(() => {
        if (!org) return;
        setCurrency((settings.currency as string) ?? "GHS");
        setTimezone((settings.timezone as string) ?? "Africa/Accra");
        setLowStockThreshold((settings.low_stock_threshold as number) ?? 10);
        setTaxInclusive((settings.tax_inclusive as boolean) ?? false);
        setEnableLoyalty((settings.enable_loyalty_program as boolean) ?? false);
        setEnablePrescriptions((settings.enable_prescriptions as boolean) ?? false);
        setEnableBatchTracking((settings.enable_batch_tracking as boolean) ?? true);
        setReceiptFooter((settings.receipt_footer as string) ?? "");
    }, [org?.id]);

    const reset = useCallback(() => {
        if (!org) return;
        setCurrency((settings.currency as string) ?? "GHS");
        setTimezone((settings.timezone as string) ?? "Africa/Accra");
        setLowStockThreshold((settings.low_stock_threshold as number) ?? 10);
        setTaxInclusive((settings.tax_inclusive as boolean) ?? false);
        setEnableLoyalty((settings.enable_loyalty_program as boolean) ?? false);
        setEnablePrescriptions((settings.enable_prescriptions as boolean) ?? false);
        setEnableBatchTracking((settings.enable_batch_tracking as boolean) ?? true);
        setReceiptFooter((settings.receipt_footer as string) ?? "");
    }, [org, settings]);

    const isDirty = org != null && (
        currency !== ((settings.currency as string) ?? "GHS") ||
        timezone !== ((settings.timezone as string) ?? "Africa/Accra") ||
        lowStockThreshold !== ((settings.low_stock_threshold as number) ?? 10) ||
        taxInclusive !== ((settings.tax_inclusive as boolean) ?? false) ||
        enableLoyalty !== ((settings.enable_loyalty_program as boolean) ?? false) ||
        enablePrescriptions !== ((settings.enable_prescriptions as boolean) ?? false) ||
        enableBatchTracking !== ((settings.enable_batch_tracking as boolean) ?? true) ||
        receiptFooter !== ((settings.receipt_footer as string) ?? "")
    );

    const handleSave = async () => {
        const data: OrganizationSettingsUpdate = {
            currency,
            timezone,
            low_stock_threshold: lowStockThreshold,
            tax_inclusive: taxInclusive,
            enable_loyalty_program: enableLoyalty,
            enable_prescriptions: enablePrescriptions,
            enable_batch_tracking: enableBatchTracking,
            receipt_footer: receiptFooter,
        };
        await updateSettings(data);
    };

    if (!org) return null;

    return (
        <div className={sectionCls}>
            <div className="px-5 py-3.5 border-b border-slate-100">
                <h3 className="text-sm font-bold text-ink">Operational Settings</h3>
                <p className="text-xs text-slate-400 mt-0.5">Currency, thresholds and feature toggles</p>
            </div>

            <div className="px-5 py-4 space-y-4">
                <FeedbackBanner
                    error={settingsMutation.error}
                    success={settingsMutation.success}
                    successMsg="Settings saved successfully"
                />

                {/* Currency + Timezone */}
                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <label className={labelCls}>
                            <DollarSign className="inline w-3 h-3 mr-1 mb-0.5" />
                            Currency
                        </label>
                        <select
                            value={currency}
                            onChange={(e) => setCurrency(e.target.value)}
                            className={`${inputCls} appearance-none`}
                        >
                            {CURRENCIES.map((c) => (
                                <option key={c} value={c}>{c}</option>
                            ))}
                        </select>
                    </div>
                    <div>
                        <label className={labelCls}>
                            <Globe className="inline w-3 h-3 mr-1 mb-0.5" />
                            Timezone
                        </label>
                        <select
                            value={timezone}
                            onChange={(e) => setTimezone(e.target.value)}
                            className={`${inputCls} appearance-none`}
                        >
                            {TIMEZONES.map((tz) => (
                                <option key={tz} value={tz}>{tz}</option>
                            ))}
                        </select>
                    </div>
                </div>

                {/* Low stock threshold */}
                <div>
                    <label className={labelCls}>Low Stock Alert Threshold</label>
                    <div className="flex items-center gap-3">
                        <input
                            type="number"
                            min={0}
                            max={1000}
                            value={lowStockThreshold}
                            onChange={(e) => setLowStockThreshold(Math.max(0, parseInt(e.target.value) || 0))}
                            className={`${inputCls} w-32`}
                        />
                        <p className="text-xs text-slate-400">
                            Trigger low-stock alerts when inventory falls below this quantity
                        </p>
                    </div>
                </div>

                {/* Feature toggles */}
                <div className="space-y-2 pt-1">
                    <ToggleRow
                        label="Tax-inclusive pricing"
                        description="Prices shown to customers include tax"
                        value={taxInclusive}
                        onChange={setTaxInclusive}
                    />
                    <ToggleRow
                        label="Loyalty programme"
                        description="Award and redeem loyalty points at point of sale"
                        value={enableLoyalty}
                        onChange={setEnableLoyalty}
                    />
                    <ToggleRow
                        label="Prescription management"
                        description="Enable prescription workflows and controlled substance tracking"
                        value={enablePrescriptions}
                        onChange={setEnablePrescriptions}
                    />
                    <ToggleRow
                        label="Batch / expiry tracking"
                        description="Track stock by batch number and expiry date (FEFO)"
                        value={enableBatchTracking}
                        onChange={setEnableBatchTracking}
                    />
                </div>

                {/* Receipt footer */}
                <div>
                    <label className={labelCls}>Receipt Footer</label>
                    <textarea
                        value={receiptFooter}
                        onChange={(e) => setReceiptFooter(e.target.value)}
                        placeholder="e.g. Thank you for your business!"
                        maxLength={500}
                        rows={2}
                        className="w-full px-3 py-2.5 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors resize-none placeholder:text-slate-400"
                    />
                    <p className="text-[10px] text-slate-400 mt-1 text-right">
                        {receiptFooter.length} / 500
                    </p>
                </div>
            </div>

            <SaveFooter loading={settingsMutation.loading} isDirty={isDirty} onSave={handleSave} onReset={reset} />
        </div>
    );
}

// ─── Main component ───────────────────────────────────────────

export function OrganizationTab() {
    const { org, stats, loading, error, refresh, orgMutation, settingsMutation, updateOrg, updateSettings } = useOrganization();

    if (loading && !org) {
        return (
            <div className="flex items-center justify-center h-full text-slate-400">
                <RefreshCw className="w-5 h-5 animate-spin mr-2" />
                Loading organisation…
            </div>
        );
    }

    if (error && !org) {
        return (
            <div className="flex flex-col items-center justify-center h-full gap-3">
                <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600">
                    <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                    {error}
                </div>
                <button
                    onClick={refresh}
                    className="px-4 py-2 text-xs font-semibold text-brand-600 border border-brand-200 rounded-xl hover:bg-brand-50 transition-colors"
                >
                    Try again
                </button>
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full min-h-0 overflow-hidden">

            {/* Stats bar */}
            {stats && (
                <div className="flex-shrink-0 border-b border-slate-200 bg-white">
                    <div className="grid grid-cols-6 divide-x divide-slate-100">
                        <StatCard icon={Building2} label="Branches" value={stats.total_branches} accent="bg-blue-50 text-blue-600" />
                        <StatCard icon={Users} label="Users" value={stats.total_users} accent="bg-violet-50 text-violet-600" />
                        <StatCard icon={Pill} label="Drugs" value={stats.total_drugs} accent="bg-green-50 text-green-600" />
                        <StatCard icon={Users} label="Customers" value={stats.total_customers} accent="bg-amber-50 text-amber-600" />
                        <StatCard icon={ShoppingBag} label="Sales Today" value={stats.total_sales_today} accent="bg-teal-50 text-teal-600" />
                        <StatCard icon={TrendingUp} label="Sales This Month" value={stats.total_sales_this_month} accent="bg-brand-50 text-brand-600" />
                    </div>
                </div>
            )}

            {/* Scrollable form area */}
            <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">
                <ProfileSection org={org} orgMutation={orgMutation} updateOrg={updateOrg} />
                <OperationalSettingsSection org={org} settingsMutation={settingsMutation} updateSettings={updateSettings} />
            </div>
        </div>
    );
}