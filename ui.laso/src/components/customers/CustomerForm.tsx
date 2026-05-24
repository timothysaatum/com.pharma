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
import { X, User, AlertCircle, Shield, Building2 } from "lucide-react";
import { customersApi, type CustomerCreate, type CustomerUpdate, type CustomerWithDetails } from "@/api/customers";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";
import { useState } from "react";

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
        if (!v.first_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["first_name"], message: "First name required for registered customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["last_name"], message: "Last name required for registered customers" });
        if (!v.phone?.trim() && !v.email?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["phone"], message: "Phone or email required for registered customers" });
    }
    if (v.customer_type === "insurance") {
        if (!v.first_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["first_name"], message: "Full name required for insurance customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["last_name"], message: "Full name required for insurance customers" });
        if (!v.insurance_provider_id?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["insurance_provider_id"], message: "Insurance provider required" });
        if (!v.insurance_member_id?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["insurance_member_id"], message: "Member ID required" });
    }
    if (v.customer_type === "corporate") {
        if (!v.first_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["first_name"], message: "Full name required for corporate customers" });
        if (!v.last_name?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["last_name"], message: "Full name required for corporate customers" });
        if (!v.preferred_contract_id?.trim()) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["preferred_contract_id"], message: "Preferred contract required for corporate customers" });
    }
});

type FormValues = z.infer<typeof schema>;

// ── Helpers ───────────────────────────────────────────────────────────────────

const CUSTOMER_TYPES = [
    { value: "walk_in", label: "Walk-in", desc: "Anonymous, no account needed" },
    { value: "registered", label: "Registered", desc: "Named account with contact info" },
    { value: "insurance", label: "Insurance", desc: "Has insurance coverage" },
    { value: "corporate", label: "Corporate", desc: "Corporate account with contract" },
];

const inputCls = "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500";
const labelCls = "block text-sm font-medium text-ink mb-1.5";
function Err({ msg }: { msg?: string }) {
    if (!msg) return null;
    return <p className="text-xs text-red-500 mt-1 flex items-center gap-1"><AlertCircle className="w-3 h-3" />{msg}</p>;
}

// ── Component ─────────────────────────────────────────────────────────────────

interface CustomerFormProps {
    customer?: CustomerWithDetails;
    onSuccess: (saved: CustomerWithDetails) => void;
    onCancel: () => void;
}

export function CustomerForm({ customer, onSuccess, onCancel }: CustomerFormProps) {
    const { user } = useAuthStore();
    const isEdit = !!customer;
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [apiError, setApiError] = useState<string | null>(null);

    const { register, handleSubmit, watch, setValue, formState: { errors } } = useForm<FormValues>({
        resolver: zodResolver(schema),
        defaultValues: {
            customer_type: (customer?.customer_type as FormValues["customer_type"]) ?? "walk_in",
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
    const needsFullInfo = watchType !== "walk_in";

    const onSubmit = async (values: FormValues) => {
        if (!user?.organization_id) return;
        setIsSubmitting(true);
        setApiError(null);

        const clean = (v: string | undefined) => v?.trim() || undefined;

        const address = (values.street || values.city || values.country)
            ? { street: clean(values.street), city: clean(values.city), country: clean(values.country) ?? "Ghana" }
            : undefined;

        try {
            let saved: CustomerWithDetails;
            if (isEdit) {
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
            setApiError(parseApiError(err));
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

                <form onSubmit={handleSubmit(onSubmit)} className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
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
                            <div className="grid grid-cols-2 gap-2">
                                {CUSTOMER_TYPES.map((t) => (
                                    <label key={t.value} className={`flex items-start gap-2 p-3 rounded-xl border cursor-pointer transition-colors ${watchType === t.value ? "border-brand-500 bg-brand-50" : "border-slate-200 hover:bg-slate-50"
                                        }`}>
                                        <input type="radio" value={t.value} {...register("customer_type")} className="mt-0.5" />
                                        <div>
                                            <p className="text-sm font-semibold text-ink">{t.label}</p>
                                            <p className="text-xs text-ink-muted">{t.desc}</p>
                                        </div>
                                    </label>
                                ))}
                            </div>
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
                                <label className={labelCls}>Insurance Provider ID <span className="text-red-500">*</span></label>
                                <input {...register("insurance_provider_id")} placeholder="Provider UUID" className={inputCls} />
                                <Err msg={errors.insurance_provider_id?.message} />
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
                                <label className={labelCls}>Preferred Contract ID <span className="text-red-500">*</span></label>
                                <input {...register("preferred_contract_id")} placeholder="Contract UUID" className={inputCls} />
                                <Err msg={errors.preferred_contract_id?.message} />
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