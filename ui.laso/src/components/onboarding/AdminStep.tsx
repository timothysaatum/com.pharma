import { useState } from "react";
import { UseFormReturn } from "react-hook-form";
import { motion } from "framer-motion";
import { User, Mail, Phone, Eye, EyeOff, ShieldCheck } from "lucide-react";
import type { OnboardingValues } from "@/lib/validators";
import { Input } from "../ui";

// Password strength calculation
function getPasswordStrength(password: string): {
    score: number;
    label: string;
    color: string;
} {
    if (!password) return { score: 0, label: "", color: "bg-slate-200" };
    let score = 0;
    if (password.length >= 8) score++;
    if (password.length >= 12) score++;
    if (/[A-Z]/.test(password)) score++;
    if (/[a-z]/.test(password)) score++;
    if (/[0-9]/.test(password)) score++;
    if (/[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]/.test(password)) score++;

    if (score <= 2) return { score, label: "Weak", color: "bg-red-400" };
    if (score <= 4) return { score, label: "Fair", color: "bg-amber-400" };
    if (score === 5) return { score, label: "Good", color: "bg-brand-400" };
    return { score, label: "Strong", color: "bg-brand-600" };
}

interface AdminStepProps {
    form: UseFormReturn<OnboardingValues>;
}

export function AdminStep({ form }: AdminStepProps) {
    const { register, watch, formState: { errors } } = form;
    const [showPassword, setShowPassword] = useState(false);
    const [showConfirm, setShowConfirm] = useState(false);

    const password = watch("password") ?? "";
    const strength = getPasswordStrength(password);

    return (
        <motion.div
            key="admin-step"
            initial={{ opacity: 0, x: 32 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -32 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            className="space-y-6"
        >
            <div>
                <h2 className="font-display text-2xl font-bold text-ink tracking-tight">
                    Admin Account
                </h2>
                <p className="text-sm text-ink-secondary mt-1">
                    This account will have full administrative access to the organization
                </p>
            </div>

            {/* Identity */}
            <div className="space-y-4">
                <p className="text-xs font-semibold uppercase tracking-widest text-ink-muted">
                    Identity
                </p>
                <div className="grid grid-cols-2 gap-4">
                    <div className="col-span-2">
                        <Input
                            label="Full Name"
                            required
                            placeholder="John Mensah"
                            leftIcon={<User className="w-4 h-4" />}
                            error={errors.full_name?.message}
                            {...register("full_name")}
                        />
                    </div>
                    <Input
                        label="Username"
                        required
                        placeholder="john_mensah"
                        error={errors.username?.message}
                        hint="Letters, numbers, _ and - only"
                        {...register("username")}
                    />
                    <Input
                        label="Employee ID"
                        placeholder="EMP-ADMIN-001"
                        error={errors.employee_id?.message}
                        {...register("employee_id")}
                    />
                </div>
                <div className="grid grid-cols-2 gap-4">
                    <div className="col-span-2">
                        <Input
                            label="Email Address"
                            required
                            type="email"
                            placeholder="admin@pharmacy.com"
                            leftIcon={<Mail className="w-4 h-4" />}
                            error={errors.admin_email?.message}
                            {...register("admin_email")}
                        />
                    </div>
                    <div className="col-span-2">
                        <Input
                            label="Phone"
                            type="tel"
                            placeholder="+233501234567"
                            leftIcon={<Phone className="w-4 h-4" />}
                            error={errors.admin_phone?.message}
                            {...register("admin_phone")}
                        />
                    </div>
                </div>
            </div>

            {/* Security */}
            <div className="border-t border-slate-100 pt-5 space-y-4">
                <p className="text-xs font-semibold uppercase tracking-widest text-ink-muted">
                    Security
                </p>

                {/* Password with strength meter */}
                <div className="space-y-2">
                    <div className="relative">
                        <Input
                            label="Password"
                            required
                            type={showPassword ? "text" : "password"}
                            placeholder="Min. 8 chars with uppercase, number & symbol"
                            error={errors.password?.message}
                            {...register("password")}
                        />
                        <button
                            type="button"
                            onClick={() => setShowPassword((v) => !v)}
                            className="absolute right-3 top-8 text-ink-muted hover:text-ink transition-colors"
                            tabIndex={-1}
                            aria-label={showPassword ? "Hide password" : "Show password"}
                        >
                            {showPassword ? (
                                <EyeOff className="w-4 h-4" />
                            ) : (
                                <Eye className="w-4 h-4" />
                            )}
                        </button>
                    </div>

                    {/* Strength bar */}
                    {password.length > 0 && (
                        <div className="space-y-1">
                            <div className="flex gap-1">
                                {[1, 2, 3, 4, 5, 6].map((i) => (
                                    <div
                                        key={i}
                                        className={`h-1 flex-1 rounded-full transition-all duration-300 ${i <= strength.score ? strength.color : "bg-slate-100"
                                            }`}
                                    />
                                ))}
                            </div>
                            {strength.label && (
                                <p className="text-xs text-ink-muted">
                                    Strength:{" "}
                                    <span
                                        className={`font-medium ${strength.score <= 2
                                            ? "text-red-500"
                                            : strength.score <= 4
                                                ? "text-amber-500"
                                                : "text-brand-600"
                                            }`}
                                    >
                                        {strength.label}
                                    </span>
                                </p>
                            )}
                        </div>
                    )}
                </div>

                <div className="relative">
                    <Input
                        label="Confirm Password"
                        required
                        type={showConfirm ? "text" : "password"}
                        placeholder="Re-enter your password"
                        error={errors.confirm_password?.message}
                        {...register("confirm_password")}
                    />
                    <button
                        type="button"
                        onClick={() => setShowConfirm((v) => !v)}
                        className="absolute right-3 top-8 text-ink-muted hover:text-ink transition-colors"
                        tabIndex={-1}
                        aria-label={showConfirm ? "Hide password" : "Show password"}
                    >
                        {showConfirm ? (
                            <EyeOff className="w-4 h-4" />
                        ) : (
                            <Eye className="w-4 h-4" />
                        )}
                    </button>
                </div>
            </div>

            {/* Privilege notice */}
            <div className="rounded-xl bg-brand-50 border border-brand-100 p-4 flex gap-3">
                <ShieldCheck className="w-5 h-5 text-brand-600 flex-shrink-0 mt-0.5" />
                <div>
                    <p className="text-sm font-semibold text-brand-800">Admin privileges</p>
                    <p className="text-xs text-brand-700 mt-0.5 leading-relaxed">
                        This account gets the <strong>admin</strong> role — full access to
                        manage users, branches, inventory, and organization settings. You
                        can create additional users with restricted roles after setup.
                    </p>
                </div>
            </div>
        </motion.div>
    );
}