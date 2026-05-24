/**
 * ContractForm.tsx
 * ================
 * Create and edit price contracts. All validation rules mirror the
 * Pydantic validators in PriceContractCreate / PriceContractUpdate exactly:
 *
 *  - Insurance: requires insurance_provider_id + copay (amount OR %)
 *  - Default: only standard type, 0% discount, applies_to_all_branches
 *  - Promotional: requires effective_to, max 365 days duration
 *  - Wholesale: requires minimum_purchase_amount
 *  - Staff: requires allowed_user_roles including manager or admin
 *  - Senior citizen: discount ≤ 15%
 *  - Branch logic: applies_to_all_branches XOR applicable_branch_ids
 */

import { useState, useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { motion } from "framer-motion";
import {
    X, FileText, AlertCircle, ChevronDown, Info,
    Shield, Building2, Calendar, DollarSign, Users,
} from "lucide-react";
import { contractsApi, type ContractCreate, type ContractUpdate, type ContractResponse, type ContractType } from "@/api/contracts";
import { useAuthStore } from "@/stores/authStore";
import { branchApi } from "@/api/branches";
import { parseApiError } from "@/api/client";
import type { Branch } from "@/types";

// ── Zod schema — mirrors Pydantic validators ──────────────────────────────────

const schema = z.object({
    contract_code: z
        .string()
        .min(2).max(50)
        .regex(/^[A-Z0-9\-]+$/, "Uppercase letters, numbers and hyphens only")
        .refine((v) => !v.startsWith("-") && !v.endsWith("-"), "Cannot start or end with hyphen")
        .refine((v) => !v.includes("--"), "Cannot contain consecutive hyphens"),
    contract_name: z.string().min(3).max(255),
    description: z.string().max(2000).optional().or(z.literal("")),
    contract_type: z.enum(["insurance", "corporate", "staff", "senior_citizen", "standard", "wholesale", "promotional"]),
    is_default_contract: z.boolean(),
    discount_percentage: z.number().min(0).max(100),
    applies_to_prescription_only: z.boolean(),
    applies_to_otc: z.boolean(),
    applies_to_all_branches: z.boolean(),
    applicable_branch_ids: z.array(z.string()).optional(),
    effective_from: z.string().min(1, "Start date required"),
    effective_to: z.string().optional().or(z.literal("")),
    requires_verification: z.boolean(),
    requires_approval: z.boolean(),
    allowed_user_roles: z.array(z.string()),
    daily_usage_limit: z.number().int().min(1).optional().nullable(),
    per_customer_usage_limit: z.number().int().min(1).optional().nullable(),
    minimum_purchase_amount: z.number().min(0).optional().nullable(),
    maximum_purchase_amount: z.number().min(0).optional().nullable(),
    maximum_discount_amount: z.number().min(0).optional().nullable(),
    // Insurance fields
    insurance_provider_id: z.string().optional().or(z.literal("")),
    copay_amount: z.number().min(0).optional().nullable(),
    copay_percentage: z.number().min(0).max(100).optional().nullable(),
    requires_preauthorization: z.boolean(),
}).superRefine((v, ctx) => {
    // Insurance validations
    if (v.contract_type === "insurance") {
        if (!v.insurance_provider_id) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["insurance_provider_id"], message: "Insurance provider required for insurance contracts" });
        }
        if (!v.copay_amount && !v.copay_percentage) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["copay_amount"], message: "Either copay amount or copay % required for insurance contracts" });
        }
        if (v.copay_amount && v.copay_percentage) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["copay_amount"], message: "Cannot specify both copay amount and copay %" });
        }
    }
    // Default contract validations
    if (v.is_default_contract) {
        if (v.contract_type !== "standard") ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["is_default_contract"], message: "Only standard contracts can be default" });
        if (v.discount_percentage !== 0) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["discount_percentage"], message: "Default contract must have 0% discount" });
        if (!v.applies_to_all_branches) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["applies_to_all_branches"], message: "Default contract must apply to all branches" });
    }
    // Promotional validations
    if (v.contract_type === "promotional") {
        if (!v.effective_to) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["effective_to"], message: "Promotional contracts must have an expiry date" });
        if (v.effective_to) {
            const days = (new Date(v.effective_to).getTime() - new Date(v.effective_from).getTime()) / 86400000;
            if (days > 365) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["effective_to"], message: "Promotional contracts cannot exceed 365 days" });
        }
    }
    // Wholesale validations
    if (v.contract_type === "wholesale") {
        if (!v.minimum_purchase_amount) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["minimum_purchase_amount"], message: "Wholesale contracts must specify minimum purchase amount" });
        if (v.discount_percentage > 30) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["discount_percentage"], message: "Wholesale discount cannot exceed 30%" });
    }
    // Senior citizen
    if (v.contract_type === "senior_citizen" && v.discount_percentage > 15) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["discount_percentage"], message: "Senior citizen discount should not exceed 15%" });
    }
    // Staff
    if (v.contract_type === "staff") {
        if (!v.allowed_user_roles.length) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["allowed_user_roles"], message: "Staff contracts must restrict which roles can apply them" });
        const hasApprover = v.allowed_user_roles.includes("manager") || v.allowed_user_roles.includes("admin");
        if (!hasApprover) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["allowed_user_roles"], message: "Staff contracts must include manager or admin role" });
    }
    // Branch logic
    if (!v.applies_to_all_branches && (!v.applicable_branch_ids || v.applicable_branch_ids.length === 0)) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["applicable_branch_ids"], message: "Select at least one branch, or enable 'applies to all branches'" });
    }
    // Drug applicability
    if (!v.applies_to_prescription_only && !v.applies_to_otc) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["applies_to_otc"], message: "Contract must apply to at least one drug type" });
    }
    // Date range
    if (v.effective_to && v.effective_from && v.effective_to < v.effective_from) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["effective_to"], message: "End date must be on or after start date" });
    }
    // Purchase amount range
    if (v.minimum_purchase_amount && v.maximum_purchase_amount) {
        if (v.minimum_purchase_amount > v.maximum_purchase_amount) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["maximum_purchase_amount"], message: "Max purchase amount must be ≥ min purchase amount" });
        }
    }
});

type FormValues = z.infer<typeof schema>;

// ── Helpers ───────────────────────────────────────────────────────────────────

const CONTRACT_TYPES: Array<{ value: ContractType; label: string; description: string }> = [
    { value: "standard", label: "Standard", description: "Default retail pricing for all customers" },
    { value: "insurance", label: "Insurance", description: "Insurance provider with copay structure" },
    { value: "corporate", label: "Corporate", description: "Bulk pricing for corporate clients" },
    { value: "staff", label: "Staff", description: "Employee discount pricing" },
    { value: "senior_citizen", label: "Senior Citizen", description: "Discounted pricing for seniors (60+)" },
    { value: "wholesale", label: "Wholesale", description: "Bulk purchase pricing with minimum order" },
    { value: "promotional", label: "Promotional", description: "Time-limited promotional pricing" },
];

const ALL_ROLES = ["super_admin", "admin", "manager", "pharmacist", "cashier"] as const;

const inputCls = "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500";
const labelCls = "block text-sm font-medium text-ink mb-1.5";
const errorCls = "text-xs text-red-500 mt-1";
const sectionCls = "space-y-4 p-4 rounded-2xl border border-slate-100 bg-slate-50/50";

function FieldError({ msg }: { msg?: string }) {
    if (!msg) return null;
    return <p className={errorCls}><AlertCircle className="w-3 h-3 inline mr-1" />{msg}</p>;
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface ContractFormProps {
    contract?: ContractResponse;
    onSuccess: (saved: ContractResponse) => void;
    onCancel: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ContractForm({ contract, onSuccess, onCancel }: ContractFormProps) {
    const { user } = useAuthStore();
    const isEdit = !!contract;
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [apiError, setApiError] = useState<string | null>(null);
    const [branches, setBranches] = useState<Branch[]>([]);

    const today = new Date().toISOString().split("T")[0];

    const {
        register, handleSubmit, watch, setValue,
        formState: { errors },
    } = useForm<FormValues>({
        resolver: zodResolver(schema),
        defaultValues: {
            contract_code: contract?.contract_code ?? "",
            contract_name: contract?.contract_name ?? "",
            description: contract?.description ?? "",
            contract_type: (contract?.contract_type as ContractType) ?? "standard",
            is_default_contract: contract?.is_default_contract ?? false,
            discount_percentage: contract?.discount_percentage ?? 0,
            applies_to_prescription_only: contract?.applies_to_prescription_only ?? false,
            applies_to_otc: contract?.applies_to_otc ?? true,
            applies_to_all_branches: contract?.applies_to_all_branches ?? true,
            applicable_branch_ids: contract?.applicable_branch_ids ?? [],
            effective_from: contract?.effective_from ?? today,
            effective_to: contract?.effective_to ?? "",
            requires_verification: contract?.requires_verification ?? false,
            requires_approval: contract?.requires_approval ?? false,
            allowed_user_roles: contract?.allowed_user_roles ?? [],
            daily_usage_limit: contract?.daily_usage_limit ?? null,
            per_customer_usage_limit: null,
            minimum_purchase_amount: contract?.minimum_purchase_amount ?? null,
            maximum_purchase_amount: contract?.maximum_purchase_amount ?? null,
            maximum_discount_amount: contract?.maximum_discount_amount ?? null,
            insurance_provider_id: contract?.insurance_provider_id ?? "",
            copay_amount: contract?.copay_amount ?? null,
            copay_percentage: contract?.copay_percentage ?? null,
            requires_preauthorization: false,
        },
    });

    const watchType = watch("contract_type");
    const watchAllBranches = watch("applies_to_all_branches");
    const watchBranchIds = watch("applicable_branch_ids") ?? [];
    const watchRoles = watch("allowed_user_roles");
    const watchIsDefault = watch("is_default_contract");

    // Load branches for the branch selector
    useEffect(() => {
        if (!user?.organization_id) return;
        branchApi.list().then((res) => setBranches(res.items ?? [])).catch(() => { });
    }, [user?.organization_id]);

    // When type changes to default-compatible, auto-set constraints
    useEffect(() => {
        if (watchIsDefault) {
            setValue("contract_type", "standard");
            setValue("discount_percentage", 0);
            setValue("applies_to_all_branches", true);
        }
    }, [watchIsDefault, setValue]);

    const toggleBranchId = (id: string) => {
        const current = watchBranchIds;
        setValue(
            "applicable_branch_ids",
            current.includes(id) ? current.filter((b) => b !== id) : [...current, id]
        );
    };

    const toggleRole = (role: string) => {
        const current = watchRoles ?? [];
        setValue(
            "allowed_user_roles",
            current.includes(role) ? current.filter((r) => r !== role) : [...current, role]
        );
    };

    const onSubmit = async (values: FormValues) => {
        if (!user?.organization_id) return;
        setIsSubmitting(true);
        setApiError(null);

        const clean = <T,>(v: T | null | undefined): T | undefined =>
            v === null || v === "" ? undefined : (v as T);

        try {
            let saved: ContractResponse;
            if (isEdit) {
                const payload: ContractUpdate = {
                    contract_name: values.contract_name,
                    description: clean(values.description),
                    discount_percentage: values.discount_percentage,
                    applies_to_prescription_only: values.applies_to_prescription_only,
                    applies_to_otc: values.applies_to_otc,
                    applies_to_all_branches: values.applies_to_all_branches,
                    applicable_branch_ids: values.applies_to_all_branches ? [] : values.applicable_branch_ids,
                    effective_to: clean(values.effective_to),
                    requires_verification: values.requires_verification,
                    requires_approval: values.requires_approval,
                    allowed_user_roles: values.allowed_user_roles,
                    daily_usage_limit: clean(values.daily_usage_limit),
                    per_customer_usage_limit: clean(values.per_customer_usage_limit),
                    minimum_purchase_amount: clean(values.minimum_purchase_amount),
                    maximum_purchase_amount: clean(values.maximum_purchase_amount),
                    maximum_discount_amount: clean(values.maximum_discount_amount),
                    copay_amount: clean(values.copay_amount),
                    copay_percentage: clean(values.copay_percentage),
                    requires_preauthorization: values.requires_preauthorization,
                };
                saved = await contractsApi.update(contract.id, payload);
            } else {
                const payload: ContractCreate = {
                    organization_id: user.organization_id,
                    contract_code: values.contract_code.toUpperCase(),
                    contract_name: values.contract_name,
                    description: clean(values.description),
                    contract_type: values.contract_type,
                    is_default_contract: values.is_default_contract,
                    discount_type: "percentage",
                    discount_percentage: values.discount_percentage,
                    applies_to_prescription_only: values.applies_to_prescription_only,
                    applies_to_otc: values.applies_to_otc,
                    applies_to_all_branches: values.applies_to_all_branches,
                    applicable_branch_ids: values.applies_to_all_branches ? [] : values.applicable_branch_ids,
                    effective_from: values.effective_from,
                    effective_to: clean(values.effective_to),
                    requires_verification: values.requires_verification,
                    requires_approval: values.requires_approval,
                    allowed_user_roles: values.allowed_user_roles,
                    daily_usage_limit: clean(values.daily_usage_limit),
                    per_customer_usage_limit: clean(values.per_customer_usage_limit),
                    minimum_purchase_amount: clean(values.minimum_purchase_amount),
                    maximum_purchase_amount: clean(values.maximum_purchase_amount),
                    maximum_discount_amount: clean(values.maximum_discount_amount),
                    insurance_provider_id: clean(values.insurance_provider_id),
                    copay_amount: clean(values.copay_amount),
                    copay_percentage: clean(values.copay_percentage),
                    requires_preauthorization: values.requires_preauthorization,
                    status: "draft",
                    is_active: false,
                };
                saved = await contractsApi.create(payload);
            }
            onSuccess(saved);
        } catch (err) {
            setApiError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm p-4">
            <motion.div
                initial={{ opacity: 0, y: 24 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 16 }}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[92vh] flex flex-col"
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 flex-shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-brand-50 flex items-center justify-center">
                            <FileText className="w-5 h-5 text-brand-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">
                                {isEdit ? `Edit — ${contract.contract_name}` : "New Price Contract"}
                            </h2>
                            <p className="text-xs text-ink-muted">
                                {isEdit ? `Code: ${contract.contract_code}` : "Contracts are created as drafts and must be approved before use"}
                            </p>
                        </div>
                    </div>
                    <button onClick={onCancel} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Form */}
                <form onSubmit={handleSubmit(onSubmit)} className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
                    {apiError && (
                        <div className="rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2">
                            <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-600">{apiError}</p>
                        </div>
                    )}

                    {/* Identity */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide flex items-center gap-1.5">
                            <FileText className="w-3.5 h-3.5" />Identity
                        </p>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>
                                    Contract Code <span className="text-red-500">*</span>
                                </label>
                                <input
                                    {...register("contract_code")}
                                    placeholder="e.g. GLICO-STD"
                                    disabled={isEdit}
                                    className={`${inputCls} uppercase ${isEdit ? "bg-slate-50 text-ink-muted cursor-not-allowed" : ""}`}
                                />
                                <FieldError msg={errors.contract_code?.message} />
                                {isEdit && (
                                    <p className="text-xs text-ink-muted mt-1 flex items-center gap-1">
                                        <Info className="w-3 h-3" />Code cannot be changed after creation
                                    </p>
                                )}
                            </div>
                            <div>
                                <label className={labelCls}>
                                    Contract Name <span className="text-red-500">*</span>
                                </label>
                                <input {...register("contract_name")} placeholder="e.g. GLICO Insurance Standard Plan" className={inputCls} />
                                <FieldError msg={errors.contract_name?.message} />
                            </div>
                        </div>
                        <div>
                            <label className={labelCls}>Description</label>
                            <textarea {...register("description")} rows={2} placeholder="Contract terms, conditions, and usage notes…" className="w-full px-3 py-2 rounded-xl border border-slate-200 text-sm text-ink resize-none focus:outline-none focus:ring-2 focus:ring-brand-500/30" />
                        </div>
                        <div>
                            <label className={labelCls}>Contract Type <span className="text-red-500">*</span></label>
                            <div className="relative">
                                <select
                                    {...register("contract_type")}
                                    disabled={isEdit}
                                    className={`${inputCls} appearance-none pr-8 ${isEdit ? "bg-slate-50 text-ink-muted cursor-not-allowed" : ""}`}
                                >
                                    {CONTRACT_TYPES.map((t) => (
                                        <option key={t.value} value={t.value}>{t.label} — {t.description}</option>
                                    ))}
                                </select>
                                <ChevronDown className="absolute right-3 top-3 w-4 h-4 text-ink-muted pointer-events-none" />
                            </div>
                            <FieldError msg={errors.contract_type?.message} />
                        </div>
                        <label className="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" {...register("is_default_contract")} className="w-4 h-4 rounded" />
                            <span className="text-sm font-medium text-ink">Set as default contract</span>
                            <span className="text-xs text-ink-muted">(standard type, 0% discount, all branches — auto-selected at POS)</span>
                        </label>
                        <FieldError msg={errors.is_default_contract?.message} />
                    </div>

                    {/* Discount */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide flex items-center gap-1.5">
                            <DollarSign className="w-3.5 h-3.5" />Discount
                        </p>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>Discount % <span className="text-red-500">*</span></label>
                                <div className="relative">
                                    <input type="number" min={0} max={100} step="0.01" {...register("discount_percentage", { valueAsNumber: true })} className={`${inputCls} pr-8`} />
                                    <span className="absolute right-3 top-2.5 text-sm text-ink-muted">%</span>
                                </div>
                                <FieldError msg={errors.discount_percentage?.message} />
                            </div>
                            <div>
                                <label className={labelCls}>Max Discount Cap (optional)</label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                    <input type="number" min={0} step="0.01" {...register("maximum_discount_amount", { valueAsNumber: true })} placeholder="No cap" className={`${inputCls} pl-7`} />
                                </div>
                            </div>
                        </div>
                        <div className="flex gap-6">
                            <label className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" {...register("applies_to_otc")} className="w-4 h-4 rounded" />
                                <span className="text-sm text-ink">Applies to OTC drugs</span>
                            </label>
                            <label className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" {...register("applies_to_prescription_only")} className="w-4 h-4 rounded" />
                                <span className="text-sm text-ink">Prescription drugs only</span>
                            </label>
                        </div>
                        <FieldError msg={errors.applies_to_otc?.message} />
                    </div>

                    {/* Purchase limits */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide">Purchase Limits</p>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>
                                    Min Purchase Amount {watchType === "wholesale" && <span className="text-red-500">*</span>}
                                </label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                    <input type="number" min={0} step="0.01" {...register("minimum_purchase_amount", { valueAsNumber: true })} placeholder="None" className={`${inputCls} pl-7`} />
                                </div>
                                <FieldError msg={errors.minimum_purchase_amount?.message} />
                            </div>
                            <div>
                                <label className={labelCls}>Max Purchase Amount</label>
                                <div className="relative">
                                    <span className="absolute left-3 top-2.5 text-sm text-ink-muted">₵</span>
                                    <input type="number" min={0} step="0.01" {...register("maximum_purchase_amount", { valueAsNumber: true })} placeholder="None" className={`${inputCls} pl-7`} />
                                </div>
                                <FieldError msg={errors.maximum_purchase_amount?.message} />
                            </div>
                        </div>
                    </div>

                    {/* Insurance section — only when type = insurance */}
                    {watchType === "insurance" && (
                        <div className={`${sectionCls} border-blue-100 bg-blue-50/40`}>
                            <p className="text-xs font-semibold text-blue-700 uppercase tracking-wide flex items-center gap-1.5">
                                <Shield className="w-3.5 h-3.5" />Insurance Details
                            </p>
                            <div>
                                <label className={labelCls}>Insurance Provider ID <span className="text-red-500">*</span></label>
                                <input {...register("insurance_provider_id")} placeholder="Insurance provider UUID" className={inputCls} />
                                <FieldError msg={errors.insurance_provider_id?.message} />
                                <p className="text-xs text-ink-muted mt-1">Enter the UUID of the insurance provider from your provider list.</p>
                            </div>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className={labelCls}>Copay Amount (₵)</label>
                                    <input type="number" min={0} step="0.01" {...register("copay_amount", { valueAsNumber: true })} placeholder="Fixed copay" className={inputCls} />
                                    <FieldError msg={errors.copay_amount?.message} />
                                </div>
                                <div>
                                    <label className={labelCls}>Copay % (of price)</label>
                                    <input type="number" min={0} max={100} step="0.01" {...register("copay_percentage", { valueAsNumber: true })} placeholder="e.g. 15" className={inputCls} />
                                </div>
                            </div>
                            <p className="text-xs text-amber-600 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
                                Specify either a fixed copay amount <em>or</em> a copay percentage — not both.
                            </p>
                            <label className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" {...register("requires_preauthorization")} className="w-4 h-4 rounded" />
                                <span className="text-sm text-ink">Requires pre-authorization from insurer</span>
                            </label>
                        </div>
                    )}

                    {/* Validity */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide flex items-center gap-1.5">
                            <Calendar className="w-3.5 h-3.5" />Validity Period
                        </p>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>
                                    Effective From <span className="text-red-500">*</span>
                                </label>
                                <input type="date" {...register("effective_from")} disabled={isEdit} className={`${inputCls} ${isEdit ? "bg-slate-50 text-ink-muted" : ""}`} />
                                <FieldError msg={errors.effective_from?.message} />
                            </div>
                            <div>
                                <label className={labelCls}>
                                    Effective To {watchType === "promotional" && <span className="text-red-500">*</span>}
                                </label>
                                <input type="date" {...register("effective_to")} className={inputCls} />
                                <FieldError msg={errors.effective_to?.message} />
                                {!watch("effective_to") && watchType !== "promotional" && (
                                    <p className="text-xs text-ink-muted mt-1">Leave blank for no expiry</p>
                                )}
                            </div>
                        </div>
                    </div>

                    {/* Branch applicability */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide flex items-center gap-1.5">
                            <Building2 className="w-3.5 h-3.5" />Branch Applicability
                        </p>
                        <label className="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" {...register("applies_to_all_branches")} className="w-4 h-4 rounded" />
                            <span className="text-sm font-medium text-ink">Applies to all branches</span>
                        </label>
                        {!watchAllBranches && (
                            <div>
                                <label className={labelCls}>Select specific branches</label>
                                {branches.length === 0 ? (
                                    <p className="text-sm text-ink-muted">Loading branches…</p>
                                ) : (
                                    <div className="space-y-1.5 max-h-40 overflow-y-auto pr-1">
                                        {branches.map((b) => (
                                            <label key={b.id} className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-white transition-colors">
                                                <input
                                                    type="checkbox"
                                                    checked={watchBranchIds.includes(b.id)}
                                                    onChange={() => toggleBranchId(b.id)}
                                                    className="w-4 h-4 rounded"
                                                />
                                                <span className="text-sm text-ink">{b.name}</span>
                                                <span className="text-xs text-ink-muted font-mono ml-auto">{b.code}</span>
                                            </label>
                                        ))}
                                    </div>
                                )}
                                <FieldError msg={errors.applicable_branch_ids?.message} />
                            </div>
                        )}
                    </div>

                    {/* Access controls */}
                    <div className={sectionCls}>
                        <p className="text-xs font-semibold text-ink-muted uppercase tracking-wide flex items-center gap-1.5">
                            <Users className="w-3.5 h-3.5" />Access &amp; Usage Controls
                        </p>
                        <div className="flex gap-6 flex-wrap">
                            <label className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" {...register("requires_verification")} className="w-4 h-4 rounded" />
                                <span className="text-sm text-ink">Requires verification (e.g. ID check)</span>
                            </label>
                            <label className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" {...register("requires_approval")} className="w-4 h-4 rounded" />
                                <span className="text-sm text-ink">Requires manager approval at checkout</span>
                            </label>
                        </div>
                        <div>
                            <label className={labelCls}>
                                Allowed Roles {watchType === "staff" && <span className="text-red-500">*</span>}
                            </label>
                            <div className="flex gap-2 flex-wrap">
                                {ALL_ROLES.map((role) => (
                                    <button
                                        key={role}
                                        type="button"
                                        onClick={() => toggleRole(role)}
                                        className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors capitalize ${(watchRoles ?? []).includes(role)
                                                ? "bg-brand-600 text-white"
                                                : "bg-white border border-slate-200 text-ink-secondary hover:text-ink"
                                            }`}
                                    >
                                        {role.replace("_", " ")}
                                    </button>
                                ))}
                            </div>
                            <p className="text-xs text-ink-muted mt-1">Leave empty to allow all roles</p>
                            <FieldError msg={errors.allowed_user_roles?.message} />
                        </div>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className={labelCls}>Daily Usage Limit</label>
                                <input type="number" min={1} {...register("daily_usage_limit", { valueAsNumber: true })} placeholder="No limit" className={inputCls} />
                            </div>
                            <div>
                                <label className={labelCls}>Per-Customer Usage Limit</label>
                                <input type="number" min={1} {...register("per_customer_usage_limit", { valueAsNumber: true })} placeholder="No limit" className={inputCls} />
                            </div>
                        </div>
                    </div>
                </form>

                {/* Footer */}
                <div className="flex justify-end gap-2 px-6 py-4 border-t border-slate-100 flex-shrink-0">
                    <button type="button" onClick={onCancel} className="px-4 py-2.5 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors">
                        Cancel
                    </button>
                    <button
                        type="submit"
                        form="contract-form"
                        onClick={handleSubmit(onSubmit)}
                        disabled={isSubmitting}
                        className="px-5 py-2.5 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2"
                    >
                        {isSubmitting && <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" /></svg>}
                        {isSubmitting ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save Changes" : "Create Contract")}
                    </button>
                </div>
            </motion.div>
        </div>
    );
}