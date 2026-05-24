import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
    Eye, EyeOff, Stethoscope, AlertTriangle,
    Lock, ArrowRight, ShieldAlert,
} from "lucide-react";
import { useLogin } from "@/hooks/useLogin";
import { BranchSelector } from "@/components/auth/BranchSelector";
import { useAuthStore } from "@/stores/authStore";
import { Input, Button } from "@/components/ui";
import { branchApi } from "@/api/branches";
import type { BranchListItem } from "@/types";

export default function LoginPage() {
    const navigate = useNavigate();
    const { isAuthenticated, user, activeBranchId, setActiveBranch } = useAuthStore();
    const [showPassword, setShowPassword] = useState(false);
    const [showBranchSelector, setShowBranchSelector] = useState(false);
    const [branches, setBranches] = useState<BranchListItem[]>([]);
    const [fetchingBranches, setFetchingBranches] = useState(false);

    // Redirect if already fully authenticated with a branch
    useEffect(() => {
        if (isAuthenticated && activeBranchId) {
            navigate("/dashboard", { replace: true });
        }
    }, [isAuthenticated, activeBranchId, navigate]);

    const { form, isSubmitting, error, isLocked, submit } = useLogin({
        onSuccess: async () => {
            const { user: loggedInUser } = useAuthStore.getState();
            if (!loggedInUser) return;

            setFetchingBranches(true);
            try {
                const fetched = await branchApi.listMine();
                setBranches(fetched);

                if (fetched.length === 1) {
                    // Single branch — auto-select and redirect immediately
                    setActiveBranch(String(fetched[0].id));
                    navigate("/dashboard", { replace: true });
                } else if (fetched.length === 0) {
                    // No branches assigned — still proceed, dashboard will handle it
                    navigate("/dashboard", { replace: true });
                } else {
                    // Multiple branches — show selector
                    setShowBranchSelector(true);
                }
            } catch {
                // If branch fetch fails, fall back to assigned_branches count
                const count = loggedInUser.assigned_branches?.length ?? 0;
                if (count <= 1) {
                    navigate("/dashboard", { replace: true });
                } else {
                    setShowBranchSelector(true);
                }
            } finally {
                setFetchingBranches(false);
            }
        },
    });

    const { register, handleSubmit, formState: { errors } } = form;

    return (
        <div className="min-h-screen flex">
            {/* ── Left panel: branding ────────────────────────── */}
            <motion.div
                initial={{ opacity: 0, x: -40 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.5 }}
                className="hidden lg:flex w-[45%] bg-brand-600 flex-col justify-between p-12 relative overflow-hidden"
            >
                {/* Background pattern */}
                <div className="absolute inset-0 opacity-10">
                    {[...Array(6)].map((_, i) => (
                        <div
                            key={i}
                            className="absolute rounded-full border-2 border-white"
                            style={{
                                width: `${(i + 2) * 120}px`,
                                height: `${(i + 2) * 120}px`,
                                top: "50%",
                                left: "50%",
                                transform: "translate(-50%, -50%)",
                            }}
                        />
                    ))}
                </div>

                {/* Logo */}
                <div className="relative flex items-center gap-3">
                    <div className="w-12 h-12 rounded-2xl bg-white/20 backdrop-blur flex items-center justify-center">
                        <Stethoscope className="w-6 h-6 text-white" />
                    </div>
                    <span className="font-display text-2xl font-bold text-white tracking-tight">
                        Laso
                    </span>
                </div>

                {/* Headline */}
                <div className="relative space-y-4">
                    <h1 className="font-display text-5xl font-bold text-white leading-tight">
                        Pharmacy
                        <br />
                        management
                        <br />
                        <span className="text-brand-200">simplified.</span>
                    </h1>
                    <p className="text-brand-100 text-lg leading-relaxed max-w-sm">
                        Everything you need to run your pharmacy — from inventory
                        to sales to prescriptions — in one place.
                    </p>
                </div>

                {/* Stats row */}
                <div className="relative grid grid-cols-3 gap-4">
                    {[
                        { value: "99.9%", label: "Uptime" },
                        { value: "< 1s", label: "Response time" },
                        { value: "256-bit", label: "Encryption" },
                    ].map((stat) => (
                        <div
                            key={stat.label}
                            className="bg-white/10 backdrop-blur rounded-2xl p-4"
                        >
                            <p className="font-display text-2xl font-bold text-white">
                                {stat.value}
                            </p>
                            <p className="text-xs text-brand-200 mt-1">{stat.label}</p>
                        </div>
                    ))}
                </div>
            </motion.div>

            {/* ── Right panel: form ───────────────────────────── */}
            <div className="flex-1 flex items-center justify-center px-6 py-12 bg-gradient-to-br from-slate-50 to-white">
                <motion.div
                    initial={{ opacity: 0, y: 24 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4, delay: 0.1 }}
                    className="w-full max-w-md"
                >
                    {/* Mobile logo */}
                    <div className="flex items-center gap-2 mb-8 lg:hidden">
                        <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center">
                            <Stethoscope className="w-4 h-4 text-white" />
                        </div>
                        <span className="font-display text-xl font-bold text-ink">Laso</span>
                    </div>

                    <AnimatePresence mode="wait">
                        {!showBranchSelector ? (
                            /* ── Login form ── */
                            <motion.div
                                key="login-form"
                                initial={{ opacity: 0, x: 20 }}
                                animate={{ opacity: 1, x: 0 }}
                                exit={{ opacity: 0, x: -20 }}
                                transition={{ duration: 0.25 }}
                                className="space-y-6"
                            >
                                <div>
                                    <h2 className="font-display text-3xl font-bold text-ink tracking-tight">
                                        Welcome back
                                    </h2>
                                    <p className="text-ink-secondary mt-1">
                                        Sign in to your Laso account
                                    </p>
                                </div>

                                {/* Account locked warning */}
                                {isLocked && (
                                    <motion.div
                                        initial={{ opacity: 0, y: -8 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        className="rounded-xl bg-red-50 border border-red-100 p-4 flex gap-3"
                                    >
                                        <ShieldAlert className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
                                        <div>
                                            <p className="text-sm font-semibold text-red-700">
                                                Account temporarily locked
                                            </p>
                                            <p className="text-xs text-red-600 mt-0.5">
                                                Too many failed attempts. Please wait a few minutes
                                                or contact your administrator.
                                            </p>
                                        </div>
                                    </motion.div>
                                )}

                                {/* Generic error */}
                                {error && !isLocked && (
                                    <motion.div
                                        initial={{ opacity: 0, y: -8 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        className="rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-start"
                                    >
                                        <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                                        <p className="text-sm text-red-600">{error}</p>
                                    </motion.div>
                                )}

                                <form onSubmit={handleSubmit(submit)} noValidate className="space-y-4">
                                    <Input
                                        label="Username"
                                        required
                                        placeholder="username"
                                        autoComplete="username"
                                        autoFocus
                                        error={errors.username?.message}
                                        {...register("username")}
                                    />

                                    <div className="relative">
                                        <Input
                                            label="Password"
                                            required
                                            type={showPassword ? "text" : "password"}
                                            placeholder="••••••••"
                                            autoComplete="current-password"
                                            error={errors.password?.message}
                                            leftIcon={<Lock className="w-4 h-4" />}
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

                                    <Button
                                        type="submit"
                                        size="lg"
                                        className="w-full mt-2"
                                        loading={isSubmitting || fetchingBranches}
                                        disabled={isSubmitting || isLocked || fetchingBranches}
                                    >
                                        {fetchingBranches
                                            ? "Loading branches…"
                                            : isSubmitting
                                                ? "Signing in…"
                                                : "Sign in"}
                                        {!isSubmitting && !fetchingBranches && <ArrowRight className="w-4 h-4" />}
                                    </Button>
                                </form>

                                <div className="pt-2 border-t border-slate-100 text-center">
                                    <p className="text-xs text-ink-muted">
                                        Don't have an account?{" "}
                                        <a
                                            href="/onboarding"
                                            className="text-brand-600 hover:text-brand-700 font-semibold hover:underline"
                                        >
                                            Set up your organization
                                        </a>
                                    </p>
                                </div>
                            </motion.div>
                        ) : (
                            /* ── Branch selector ── */
                            <motion.div
                                key="branch-selector"
                                initial={{ opacity: 0, x: 20 }}
                                animate={{ opacity: 1, x: 0 }}
                                exit={{ opacity: 0, x: -20 }}
                                transition={{ duration: 0.25 }}
                            >
                                <BranchSelector
                                    user={user!}
                                    branches={branches}
                                    onSelect={() => navigate("/dashboard", { replace: true })}
                                />
                            </motion.div>
                        )}
                    </AnimatePresence>
                </motion.div>
            </div>
        </div>
    );
}