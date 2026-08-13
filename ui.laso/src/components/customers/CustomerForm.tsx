/**
 * CustomerForm.tsx
 * ================
 * Create and edit customers. Validation mirrors CustomerCreate Pydantic:
 *  - registered: requires first_name + last_name + (phone or email)
 *  - insurance: requires first_name + last_name + insurance_provider_id + member_id
 *  - corporate: requires first_name + last_name + preferred_contract_id
 *  - walk_in: all optional
 */

import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { motion } from "framer-motion";
import { X, User, AlertCircle, Shield, Building2, ChevronDown, Loader2 } from "lucide-react";
import { customersApi, type CustomerCreate, type CustomerUpdate, type CustomerWithDetails } from "@/api/customers";
import { contractsApi, type ContractResponse } from "@/api/contracts";
import { InsuranceProviderSelector } from "@/components/contracts/InsuranceProviderSelector";
import { useAuthStore } from "@/stores/authStore";
import { isBackendReachable, isOfflineError, parseApiError } from "@/api/client";
import { toast } from "sonner";
import { writeLocal } from "@/lib/localWrite";
import { useCallback, useEffect, useMemo, useState } from "react";

// ── Zod schema ────────────────────────────────────────────────────────────────

const schema = z.object({
    customer_type: z.enum(["walk_in", "registered", "insurance", "corporate"]),
    first_name: z.string().max(255).optional().or(z.literal("")),
    last_name: z.string().max(255).optional().or(z.literal("")),
    phone: z.string().optional().or(z.literal("")),
    email: z.string().email("Invalid email").optional().or(z.literal("")),
    date_of_birth: z.string().optional().or(z.literal("")),
    insurance_provider_id: z.string().optional().or(z.literal("")),
    insurance_member_id: z.string().max(100).optional().or(z.literal("")),
    preferred_contract_id: z.string().optional().or(z.literal("")),
    preferred_contact_method: z.enum(["email", "phone", "sms"]),
    marketing_consent: z.boolean(),
    street: z.string().optional().or(z.literal("")),
    city: z.string().optional().or(z.literal("")),
    country: z.string().optional().or(z.literal("")),
}).superRefine((v, ctx) => {
    if (v.customer_type === "registered") {
        if (!v.first_name?.trim()) ctx.addIssue({ code: "custom", path: ["first_name"], message: "First name required for registered customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: "custom", path: ["last_name"], message: "Last name required for registered customers" });
        if (!v.phone?.trim() && !v.email?.trim()) ctx.addIssue({ code: "custom", path: ["phone"], message: "Phone or email required for registered customers" });
    }
    if (v.customer_type === "insurance") {
        if (!v.first_name?.trim()) ctx.addIssue({ code: "custom", path: ["first_name"], message: "Full name required for insurance customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: "custom", path: ["last_name"], message: "Full name required for insurance customers" });
        if (!v.insurance_provider_id?.trim()) ctx.addIssue({ code: "custom", path: ["insurance_provider_id"], message: "Insurance provider required" });
        if (!v.insurance_member_id?.trim()) ctx.addIssue({ code: "custom", path: ["insurance_member_id"], message: "Member ID required" });
    }
    if (v.customer_type === "corporate") {
        if (!v.first_name?.trim()) ctx.addIssue({ code: "custom", path: ["first_name"], message: "Full name required for corporate customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: "custom", path: ["last_name"], message: "Full name required for corporate customers" });
        if (!v.preferred_contract_id?.trim()) ctx.addIssue({ code: "custom", path: ["preferred_contract_id"], message: "Preferred contract required for corporate customers" });
    }
});

type FormValues = z.infer<typeof schema>;

// ── Helpers ───────────────────────────────────────────────────────────────────

const CUSTOMER_TYPES = [
    { value: "registered", label: "Registered" },
    { value: "insurance", label: "Insurance" },
    { value: "corporate", label: "Corporate" },
] as const;

const inputCls = "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500";
const labelCls = "block text-sm font-medium text-ink mb-1.5";
function Err({ msg }: { msg?: string }) {
    if (!msg) return null;
    return <p className="text-xs text-red-500 mt-1 flex items-center gap-1"><AlertCircle className="w-3 h-3" />{msg}</p>;
}

function CorporateContractSelector({
    value,
    onChange,
    error,
}: {
    value: string | null;
    onChange: (id: string | null) => void;
    error?: string;
}) {
    const [contracts, setContracts] = useState<ContractResponse[]>([]);
    const [searchQuery, setSearchQuery] = useState("");
    const [showDropdown, setShowDropdown] = useState(false);
    const [loading, setLoading] = useState(false);

    const loadContracts = useCallback(async () => {
        setLoading(true);
        try {
            const result = await contractsApi.list({
                contract_type: "corporate",
                status: "active",
                is_active: true,
                search: searchQuery,
                page_size: 25,
            });
            setContracts(result.contracts);
        } catch (err) {
            toast.error("Failed to load corporate contracts. " + parseApiError(err));
            setContracts([]);
        } finally {
            setLoading(false);
        }
    }, [searchQuery]);

    useEffect(() => {
        const timer = setTimeout(() => void loadContracts(), 300);
        return () => clearTimeout(timer);
    }, [loadContracts]);

    const selectedContract = useMemo(
        () => contracts.find((contract) => contract.id === value) ?? null,
        [contracts, value]
    );

    return (
        <div className="space-y-2">
            <div className="relative">
                <button
                    type="button"
                    onClick={() => setShowDropdown((open) => !open)}
                    className={`${inputCls} flex items-center justify-between cursor-pointer ${error ? "border-red-300 bg-red-50/30" : ""}`}
                >
                    <span className="text-left min-w-0">
                        {selectedContract ? (
                            <span className="block min-w-0">
                                <span className="block font-semibold text-ink truncate">{selectedContract.contract_name}</span>
                                <span className="block text-xs text-ink-muted truncate">
                                    {selectedContract.contract_code} · {selectedContract.discount_percentage}% discount
                                </span>
                            </span>
                        ) : (
                            <span className="text-slate-400">Select corporate contract...</span>
                        )}
                    </span>
                    <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${showDropdown ? "rotate-180" : ""}`} />
                </button>

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

                {showDropdown && (
                    <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-lg z-50">
                        <div className="p-2 border-b border-slate-100">
                            <input
                                type="text"
                                placeholder="Search contracts..."
                                value={searchQuery}
                                onChange={(e) => setSearchQuery(e.target.value)}
                                className={inputCls}
                                autoFocus
                            />
                        </div>

                        {loading ? (
                            <div className="p-3 text-center text-slate-500">
                                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                                Loading contracts...
                            </div>
                        ) : contracts.length > 0 ? (
                            <div className="max-h-64 overflow-y-auto">
                                {contracts.map((contract) => (
                                    <button
                                        key={contract.id}
                                        type="button"
                                        onClick={() => {
                                            onChange(contract.id);
                                            setShowDropdown(false);
                                        }}
                                        className={`w-full px-3 py-2.5 text-left text-sm hover:bg-brand-50 border-b border-slate-50 last:border-b-0 ${
                                            value === contract.id ? "bg-brand-50 border-l-2 border-l-brand-500" : ""
                                        }`}
                                    >
                                        <span className="block font-semibold text-ink truncate">{contract.contract_name}</span>
                                        <span className="block text-xs text-slate-500 truncate">
                                            {contract.contract_code} · {contract.discount_percentage}% discount
                                        </span>
                                    </button>
                                ))}
                            </div>
                        ) : (
                            <div className="p-3 text-center text-slate-500 text-sm">
                                No active corporate contracts found
                            </div>
                        )}
                    </div>
                )}
            </div>

            {error && (
                <p className="text-xs text-red-600 flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" />
                    {error}
                </p>
            )}

            <p className="text-xs text-slate-500">Search and select an active corporate contract.</p>
        </div>
    );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface CustomerFormProps {
    customer?: CustomerWithDetails;
    onSuccess: (saved: CustomerWithDetails) => void;
    onCancel: () => void;
}

export function CustomerForm({ customer, onSuccess, onCancel }: CustomerFormProps) {
    const { user, activeBranchId } = useAuthStore();
    const isEdit = !!customer;
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [apiError, setApiError] = useState<string | null>(null);

    const { register, handleSubmit, watch, setValue, formState: { errors } } = useForm<FormValues>({
        resolver: zodResolver(schema),
        defaultValues: {
            customer_type: (customer?.customer_type as FormValues["customer_type"]) ?? "registered",
            first_name: customer?.first_name ?? "",
            last_name: customer?.last_name ?? "",
            phone: customer?.phone ?? "",
            email: customer?.email ?? "",
            date_of_birth: customer?.date_of_birth ?? "",
            insurance_provider_id: customer?.insurance_provider_id ?? "",
            insurance_member_id: customer?.insurance_member_id ?? "",
            preferred_contract_id: customer?.preferred_contract_id ?? "",
            preferred_contact_method: (customer?.preferred_contact_method as "email" | "phone" | "sms") ?? "email",
            marketing_consent: customer?.marketing_consent ?? false,
            street: (customer?.address as Record<string, string> | null)?.street ?? "",
            city: (customer?.address as Record<string, string> | null)?.city ?? "",
            country: (customer?.address as Record<string, string> | null)?.country ?? "Ghana",
        },
    });

    const watchType = watch("customer_type");
    const selectedInsuranceProviderId = watch("insurance_provider_id");
    const selectedContractId = watch("preferred_contract_id");
    const needsFullInfo = watchType !== "walk_in";

    const onSubmit = async (values: FormValues) => {
        if (!user?.organization_id) return;
        setIsSubmitting(true);
        setApiError(null);

        const clean = (v: string | undefined) => v?.trim() || undefined;

        const address = (values.street || values.city || values.country)
            ? { street: clean(values.street), city: clean(values.city), country: clean(values.country) ?? "Ghana" }
            : undefined;
        const now = new Date().toISOString();
        const buildLocalCustomer = (id: string): CustomerWithDetails => ({
            id,
            organization_id: user.organization_id,
            customer_type: values.customer_type,
            first_name: clean(values.first_name) ?? null,
            last_name: clean(values.last_name) ?? null,
            phone: clean(values.phone) ?? null,
            email: clean(values.email) ?? null,
            date_of_birth: clean(values.date_of_birth) ?? null,
            address: address ?? null,
            allergies: customer?.allergies ?? [],
            chronic_conditions: customer?.chronic_conditions ?? [],
            loyalty_points: customer?.loyalty_points ?? 0,
            loyalty_tier: customer?.loyalty_tier ?? "bronze",
            total_orders: customer?.total_orders ?? 0,
            total_value: customer?.total_value ?? 0,
            preferred_contact_method: values.preferred_contact_method,
            marketing_consent: values.marketing_consent,
            is_active: customer?.is_active ?? true,
            insurance_provider_id: clean(values.insurance_provider_id) ?? null,
            insurance_member_id: clean(values.insurance_member_id) ?? null,
            insurance_card_image_url: customer?.insurance_card_image_url ?? null,
            preferred_contract_id: clean(values.preferred_contract_id) ?? null,
            is_deleted: false,
            deleted_at: null,
            deleted_by: null,
            sync_status: "pending",
            sync_version: customer?.sync_version ?? 1,
            synced_at: customer?.synced_at ?? null,
            created_at: customer?.created_at ?? now,
            updated_at: now,
            insurance_provider_name: customer?.insurance_provider_name ?? null,
            insurance_provider_code: customer?.insurance_provider_code ?? null,
            preferred_contract_name: customer?.preferred_contract_name ?? null,
            preferred_contract_discount: customer?.preferred_contract_discount ?? null,
            total_purchases: customer?.total_purchases ?? 0,
            total_spent: customer?.total_spent ?? 0,
            last_purchase_date: customer?.last_purchase_date ?? null,
        });

        try {
            let saved: CustomerWithDetails;
            if (isEdit) {
                if (!navigator.onLine || !isBackendReachable()) {
                    saved = buildLocalCustomer(customer.id);
                    await writeLocal.customer(saved, "update", activeBranchId ?? undefined);
                    onSuccess(saved);
                    return;
                }
                const payload: CustomerUpdate = {
                    first_name: clean(values.first_name),
                    last_name: clean(values.last_name),
                    phone: clean(values.phone),
                    email: clean(values.email),
                    date_of_birth: clean(values.date_of_birth),
                    address,
                    insurance_provider_id: clean(values.insurance_provider_id),
                    insurance_member_id: clean(values.insurance_member_id),
                    preferred_contract_id: clean(values.preferred_contract_id),
                    preferred_contact_method: values.preferred_contact_method,
                    marketing_consent: values.marketing_consent,
                };
                saved = await customersApi.update(customer.id, payload);
            } else {
                if (!navigator.onLine || !isBackendReachable()) {
                    saved = buildLocalCustomer(crypto.randomUUID());
                    await writeLocal.customer(saved, "create", activeBranchId ?? undefined);
                    onSuccess(saved);
                    return;
                }
                const payload: CustomerCreate = {
                    organization_id: user.organization_id,
                    customer_type: values.customer_type,
                    first_name: clean(values.first_name),
                    last_name: clean(values.last_name),
                    phone: clean(values.phone),
                    email: clean(values.email),
                    date_of_birth: clean(values.date_of_birth),
                    address,
                    insurance_provider_id: clean(values.insurance_provider_id),
                    insurance_member_id: clean(values.insurance_member_id),
                    preferred_contract_id: clean(values.preferred_contract_id),
                    preferred_contact_method: values.preferred_contact_method,
                    marketing_consent: values.marketing_consent,
                };
                saved = await customersApi.create(payload);
            }
            onSuccess(saved);
        } catch (err) {
            if (isOfflineError(err)) {
                const id = customer?.id ?? crypto.randomUUID();
                const saved = buildLocalCustomer(id);
                await writeLocal.customer(saved, isEdit ? "update" : "create", activeBranchId ?? undefined);
                onSuccess(saved);
            } else {
                setApiError(parseApiError(err));
            }
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm">
            <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 12 }}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] flex flex-col"
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 flex-shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-brand-50 flex items-center justify-center">
                            <User className="w-5 h-5 text-brand-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">
                                {isEdit ? `Edit — ${customer.first_name ?? "Customer"}` : "New Customer"}
                            </h2>
                            <p className="text-xs text-ink-muted">
                                {isEdit ? `${customer.customer_type} · ${customer.loyalty_tier} tier` : "Register a new customer"}
                            </p>
                        </div>
                    </div>
                    <button onClick={onCancel} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <form id="customer-form" onSubmit={handleSubmit(onSubmit)} className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
                    {apiError && (
                        <div className="rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2">
                            <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-600">{apiError}</p>
                        </div>
                    )}

                    {/* Customer type */}
                    {!isEdit && (
                        <div>
                            <label className={labelCls}>Customer Type <span className="text-red-500">*</span></label>
                            <select
                                {...register("customer_type", {
                                    onChange: (event) => {
                                        const nextType = event.target.value as FormValues["customer_type"];
                                        if (nextType !== "insurance") {
                                            setValue("insurance_provider_id", "");
                                            setValue("insurance_member_id", "");
                                        }
                                        if (nextType !== "corporate") {
                                            setValue("preferred_contract_id", "");
                                        }
                                    },
                                })}
                                className={inputCls}
                            >
                                {CUSTOMER_TYPES.map((t) => (
                                    <option key={t.value} value={t.value}>{t.label}</option>
                                ))}
                            </select>
                            <Err msg={errors.customer_type?.message} />
                        </div>
                    )}

                    {/* Personal info */}
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={labelCls}>
                                First Name {needsFullInfo && <span className="text-red-500">*</span>}
                            </label>
                            <input {...register("first_name")} placeholder="First name" className={inputCls} />
                            <Err msg={errors.first_name?.message} />
                        </div>
                        <div>
                            <label className={labelCls}>
                                Last Name {needsFullInfo && <span className="text-red-500">*</span>}
                            </label>
                            <input {...register("last_name")} placeholder="Last name" className={inputCls} />
                            <Err msg={errors.last_name?.message} />
                        </div>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={labelCls}>
                                Phone {watchType === "registered" && <span className="text-red-500">*</span>}
                            </label>
                            <input {...register("phone")} placeholder="+233 20 000 0000" className={inputCls} />
                            <Err msg={errors.phone?.message} />
                        </div>
                        <div>
                            <label className={labelCls}>
                                Email {watchType === "registered" && <span className="text-red-500">*</span>}
                            </label>
                            <input {...register("email")} type="email" placeholder="name@example.com" className={inputCls} />
                            <Err msg={errors.email?.message} />
                        </div>
                    </div>

                    {needsFullInfo && (
                        <div>
                            <label className={labelCls}>Date of Birth</label>
                            <input type="date" {...register("date_of_birth")} className={inputCls} />
                        </div>
                    )}

                    {/* Insurance section */}
                    {(watchType === "insurance") && (
                        <div className="space-y-3 p-4 rounded-xl border border-blue-100 bg-blue-50/40">
                            <p className="text-xs font-semibold text-blue-700 uppercase tracking-wide flex items-center gap-1.5">
                                <Shield className="w-3.5 h-3.5" />Insurance
                            </p>
                            <div>
                                <label className={labelCls}>Insurance Provider <span className="text-red-500">*</span></label>
                                <InsuranceProviderSelector
                                    value={selectedInsuranceProviderId || null}
                                    onChange={(id) => setValue("insurance_provider_id", id || "", { shouldDirty: true, shouldValidate: true })}
                                    error={errors.insurance_provider_id?.message}
                                    allowCreate={false}
                                />
                            </div>
                            <div>
                                <label className={labelCls}>Member ID <span className="text-red-500">*</span></label>
                                <input {...register("insurance_member_id")} placeholder="e.g. MEM-123456" className={inputCls} />
                                <Err msg={errors.insurance_member_id?.message} />
                            </div>
                        </div>
                    )}

                    {/* Corporate section */}
                    {watchType === "corporate" && (
                        <div className="space-y-3 p-4 rounded-xl border border-purple-100 bg-purple-50/40">
                            <p className="text-xs font-semibold text-purple-700 uppercase tracking-wide flex items-center gap-1.5">
                                <Building2 className="w-3.5 h-3.5" />Corporate
                            </p>
                            <div>
                                <label className={labelCls}>Preferred Contract <span className="text-red-500">*</span></label>
                                <CorporateContractSelector
                                    value={selectedContractId || null}
                                    onChange={(id) => setValue("preferred_contract_id", id || "", { shouldDirty: true, shouldValidate: true })}
                                    error={errors.preferred_contract_id?.message}
                                />
                            </div>
                        </div>
                    )}

                    {/* Address */}
                    {needsFullInfo && (
                        <div className="space-y-3">
                            <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide">Address (optional)</p>
                            <input {...register("street")} placeholder="Street address" className={inputCls} />
                            <div className="grid grid-cols-2 gap-3">
                                <input {...register("city")} placeholder="City" className={inputCls} />
                                <input {...register("country")} placeholder="Country" className={inputCls} />
                            </div>
                        </div>
                    )}

                    {/* Preferences */}
                    <div className="space-y-3 pt-1">
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide">Preferences</p>
                        <div>
                            <label className={labelCls}>Preferred Contact Method</label>
                            <div className="flex gap-3">
                                {(["email", "phone", "sms"] as const).map((m) => (
                                    <label key={m} className="flex items-center gap-1.5 cursor-pointer">
                                        <input type="radio" value={m} {...register("preferred_contact_method")} />
                                        <span className="text-sm capitalize text-ink">{m}</span>
                                    </label>
                                ))}
                            </div>
                        </div>
                        <label className="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" {...register("marketing_consent")} className="w-4 h-4 rounded" />
                            <span className="text-sm text-ink">Consent to marketing communications</span>
                        </label>
                    </div>
                </form>

                {/* Footer */}
                <div className="flex justify-end gap-2 px-6 py-4 border-t border-slate-100 flex-shrink-0">
                    <button type="button" onClick={onCancel} className="px-4 py-2.5 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                        Cancel
                    </button>
                    <button
                        type="submit"
                        form="customer-form"
                        onClick={handleSubmit(onSubmit)}
                        disabled={isSubmitting}
                        className="px-5 py-2.5 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2"
                    >
                        {isSubmitting && <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" /></svg>}
                        {isSubmitting ? (isEdit ? "Saving…" : "Registering…") : (isEdit ? "Save Changes" : "Register Customer")}
                    </button>
                </div>
            </motion.div>
        </div>
    );
}
