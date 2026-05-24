import { UseFormReturn } from "react-hook-form";
import { motion } from "framer-motion";
import { Building2, Phone, Mail, Globe, ShieldCheck, Landmark } from "lucide-react";
import type { OnboardingValues } from "@/lib/validators";
import { Input, Select } from "../ui";

const ORG_TYPES = [
    { value: "pharmacy", label: "Pharmacy" },
    { value: "otc", label: "Over-The-Counter (OTC)" },
    { value: "hospital_pharmacy", label: "Hospital Pharmacy" },
    { value: "chain", label: "Chain Pharmacy" },
];

const TIERS = [
    { value: "basic", label: "Basic — Get started" },
    { value: "professional", label: "Professional — Growing teams" },
    { value: "enterprise", label: "Enterprise — Full scale" },
];

const CURRENCIES = [
    { value: "GHS", label: "GHS — Ghana Cedi" },
    { value: "USD", label: "USD — US Dollar" },
    { value: "EUR", label: "EUR — Euro" },
    { value: "GBP", label: "GBP — British Pound" },
    { value: "NGN", label: "NGN — Nigerian Naira" },
    { value: "KES", label: "KES — Kenyan Shilling" },
    { value: "ZAR", label: "ZAR — South African Rand" },
];

const TIMEZONES = [
    { value: "Africa/Accra", label: "Africa/Accra (GMT+0)" },
    { value: "Africa/Lagos", label: "Africa/Lagos (GMT+1)" },
    { value: "Africa/Nairobi", label: "Africa/Nairobi (GMT+3)" },
    { value: "Africa/Johannesburg", label: "Africa/Johannesburg (GMT+2)" },
    { value: "Europe/London", label: "Europe/London" },
    { value: "America/New_York", label: "America/New_York (EST)" },
    { value: "UTC", label: "UTC" },
];

interface OrgStepProps {
    form: UseFormReturn<OnboardingValues>;
}

export function OrgStep({ form }: OrgStepProps) {
    const { register, formState: { errors } } = form;

    return (
        <motion.div
            key="org-step"
            initial={{ opacity: 0, x: 32 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -32 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            className="space-y-6">
            <div>
                <h2 className="font-display text-2xl font-bold text-ink tracking-tight">
                    Organization Details
                </h2>
                <p className="text-sm text-ink-secondary mt-1">
                    Set up your pharmacy's core information and configuration
                </p>
            </div>

            {/* Core identity */}
            <div className="space-y-4">
                <Input
                    label="Organization Name"
                    required
                    placeholder="e.g. HealthCare Plus Pharmacy Ltd"
                    leftIcon={<Building2 className="w-4 h-4" />}
                    error={errors.name?.message}
                    {...register("name")}
                />

                <div className="grid grid-cols-2 gap-4">
                    <Select
                        label="Organization Type"
                        required
                        options={ORG_TYPES}
                        placeholder="Select type…"
                        error={errors.type?.message}
                        {...register("type")}
                    />
                    <Select
                        label="Subscription Tier"
                        required
                        options={TIERS}
                        error={errors.subscription_tier?.message}
                        {...register("subscription_tier")}
                    />
                </div>
            </div>

            {/* Legal & contact */}
            <div className="border-t border-slate-100 pt-5 space-y-4">
                <p className="text-xs font-semibold uppercase tracking-widest text-ink-muted">
                    Legal & Contact
                </p>
                <div className="grid grid-cols-2 gap-4">
                    <Input
                        label="License Number"
                        placeholder="PHR-2026-001"
                        leftIcon={<ShieldCheck className="w-4 h-4" />}
                        error={errors.license_number?.message}
                        {...register("license_number")}
                    />
                    <Input
                        label="Tax ID"
                        placeholder="TAX123456789"
                        leftIcon={<Landmark className="w-4 h-4" />}
                        error={errors.tax_id?.message}
                        {...register("tax_id")}
                    />
                    <Input
                        label="Phone"
                        type="tel"
                        placeholder="+233501234567"
                        leftIcon={<Phone className="w-4 h-4" />}
                        error={errors.org_phone?.message}
                        {...register("org_phone")}
                    />
                    <Input
                        label="Email"
                        type="email"
                        placeholder="info@pharmacy.com"
                        leftIcon={<Mail className="w-4 h-4" />}
                        error={errors.org_email?.message}
                        {...register("org_email")}
                    />
                </div>
            </div>

            {/* Regional settings */}
            <div className="border-t border-slate-100 pt-5 space-y-4">
                <p className="text-xs font-semibold uppercase tracking-widest text-ink-muted">
                    Regional Settings
                </p>
                <div className="grid grid-cols-2 gap-4">
                    <Select
                        label="Currency"
                        required
                        options={CURRENCIES}
                        error={errors.currency?.message}
                        {...register("currency")}
                    />
                    <Select
                        label="Timezone"
                        required
                        options={TIMEZONES}
                        error={errors.timezone?.message}
                        {...register("timezone")}
                    />
                </div>
            </div>

            {/* Address */}
            <div className="border-t border-slate-100 pt-5 space-y-3">
                <p className="text-xs font-semibold uppercase tracking-widest text-ink-muted">
                    Address <span className="normal-case font-normal text-ink-muted">(optional)</span>
                </p>
                <div className="grid grid-cols-2 gap-4">
                    <div className="col-span-2">
                        <Input
                            placeholder="Street address"
                            leftIcon={<Globe className="w-4 h-4" />}
                            {...register("address.street")}
                        />
                    </div>
                    <Input placeholder="City" {...register("address.city")} />
                    <Input placeholder="Region / State" {...register("address.state")} />
                </div>
            </div>
        </motion.div>
    );
}