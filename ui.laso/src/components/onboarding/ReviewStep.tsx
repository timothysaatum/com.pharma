import { UseFormReturn } from "react-hook-form";
import { motion } from "framer-motion";
import {
    Building2, User, MapPin, ShieldCheck,
    CheckCircle2, AlertCircle, Edit2
} from "lucide-react";
import type { OnboardingValues } from "@/lib/validators";
import { Badge } from "../ui";

const TIER_LABELS: Record<string, string> = {
    basic: "Basic",
    professional: "Professional",
    enterprise: "Enterprise",
};

const TYPE_LABELS: Record<string, string> = {
    pharmacy: "Pharmacy",
    otc: "Over-The-Counter",
    hospital_pharmacy: "Hospital Pharmacy",
    chain: "Chain Pharmacy",
};

interface ReviewRowProps {
    label: string;
    value?: string | null;
    fallback?: string;
}

function ReviewRow({ label, value, fallback = "—" }: ReviewRowProps) {
    return (
        <div className="flex items-start justify-between py-2 gap-4">
            <span className="text-xs text-ink-muted flex-shrink-0 w-32">{label}</span>
            <span className="text-sm text-ink text-right font-medium break-all">
                {value || fallback}
            </span>
        </div>
    );
}

interface ReviewSectionProps {
    title: string;
    icon: React.ReactNode;
    onEdit: () => void;
    children: React.ReactNode;
}

function ReviewSection({ title, icon, onEdit, children }: ReviewSectionProps) {
    return (
        <div className="rounded-2xl border border-slate-100 bg-slate-50/50 overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 bg-white border-b border-slate-100">
                <div className="flex items-center gap-2">
                    <span className="text-brand-600">{icon}</span>
                    <span className="text-sm font-semibold text-ink">{title}</span>
                </div>
                <button
                    type="button"
                    onClick={onEdit}
                    className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 font-medium"
                >
                    <Edit2 className="w-3 h-3" />
                    Edit
                </button>
            </div>
            <div className="px-4 divide-y divide-slate-100">{children}</div>
        </div>
    );
}

interface ReviewStepProps {
    form: UseFormReturn<OnboardingValues>;
    onGoToStep: (step: number) => void;
    submitError: string | null;
}

export function ReviewStep({ form, onGoToStep, submitError }: ReviewStepProps) {
    const values = form.getValues();

    return (
        <motion.div
            key="review-step"
            initial={{ opacity: 0, x: 32 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -32 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            className="space-y-5"
        >
            <div>
                <h2 className="font-display text-2xl font-bold text-ink tracking-tight">
                    Review & Confirm
                </h2>
                <p className="text-sm text-ink-secondary mt-1">
                    Check the details below before creating your organization
                </p>
            </div>

            {submitError && (
                <div className="rounded-xl bg-red-50 border border-red-100 p-4 flex gap-3">
                    <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
                    <div>
                        <p className="text-sm font-semibold text-red-700">Submission failed</p>
                        <p className="text-sm text-red-600 mt-0.5">{submitError}</p>
                    </div>
                </div>
            )}

            {/* Organization */}
            <ReviewSection
                title="Organization"
                icon={<Building2 className="w-4 h-4" />}
                onEdit={() => onGoToStep(0)}
            >
                <ReviewRow label="Name" value={values.name} />
                <ReviewRow label="Type" value={TYPE_LABELS[values.type]} />
                <ReviewRow label="Tier" value={TIER_LABELS[values.subscription_tier]} />
                <ReviewRow label="License" value={values.license_number} />
                <ReviewRow label="Tax ID" value={values.tax_id} />
                <ReviewRow label="Phone" value={values.org_phone} />
                <ReviewRow label="Email" value={values.org_email} />
                <ReviewRow label="Currency" value={values.currency} />
                <ReviewRow label="Timezone" value={values.timezone} />
                {values.address?.city && (
                    <ReviewRow
                        label="Location"
                        value={[values.address.city, values.address.state]
                            .filter(Boolean)
                            .join(", ")}
                    />
                )}
            </ReviewSection>

            {/* Admin */}
            <ReviewSection
                title="Admin Account"
                icon={<User className="w-4 h-4" />}
                onEdit={() => onGoToStep(1)}
            >
                <ReviewRow label="Full Name" value={values.full_name} />
                <ReviewRow label="Username" value={values.username} />
                <ReviewRow label="Email" value={values.admin_email} />
                <ReviewRow label="Phone" value={values.admin_phone} />
                <ReviewRow label="Employee ID" value={values.employee_id} />
                <div className="flex items-center justify-between py-2 gap-4">
                    <span className="text-xs text-ink-muted w-32">Password</span>
                    <Badge variant="success" className="text-xs">
                        <ShieldCheck className="w-3 h-3 mr-1" />
                        Set & secure
                    </Badge>
                </div>
            </ReviewSection>

            {/* Branches */}
            <ReviewSection
                title="Branches"
                icon={<MapPin className="w-4 h-4" />}
                onEdit={() => onGoToStep(2)}
            >
                {values.branches && values.branches.length > 0 ? (
                    values.branches.map((branch, i) => (
                        <div key={i} className="py-3">
                            <div className="flex items-start gap-2">
                                <div className="w-5 h-5 rounded-md bg-brand-100 flex items-center justify-center flex-shrink-0 mt-0.5">
                                    <span className="text-[10px] font-bold text-brand-700">{i + 1}</span>
                                </div>
                                <div className="flex-1 min-w-0">
                                    <p className="text-sm font-medium text-ink">{branch.name}</p>
                                    
                                    <div className="flex items-center gap-2 mt-1 flex-wrap">
                                        {branch.code && (
                                            <span className="text-xs font-mono text-brand-700 bg-brand-50 px-1.5 py-0.5 rounded">
                                                {branch.code}
                                            </span>
                                        )}
                                        {branch.address?.city && (
                                            <span className="text-xs text-ink-muted">
                                                {branch.address.city}
                                            </span>
                                        )}
                                        {branch.branch_phone && (
                                            <span className="text-xs text-ink-muted">
                                                · {branch.branch_phone}
                                            </span>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </div>
                    ))
                ) : (
                    <div className="py-2 flex items-center gap-2">
                        <CheckCircle2 className="w-4 h-4 text-amber-500" />
                        <span className="text-sm text-ink-secondary">
                            A default <strong>Main Branch</strong> will be created automatically
                        </span>
                    </div>
                )}
            </ReviewSection>

            {/* Confirmation note */}
            <div className="rounded-xl bg-brand-50 border border-brand-100 p-4 flex gap-3">
                <CheckCircle2 className="w-5 h-5 text-brand-600 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-brand-700">
                    By clicking <strong>Create Organization</strong>, you confirm that all
                    details are correct. The admin account will be created with an{" "}
                    <strong>admin</strong> role. You can update any of this information
                    from the organization settings after setup.
                </p>
            </div>
        </motion.div>
    );
}