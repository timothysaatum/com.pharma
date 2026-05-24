import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import {
    Building2, MapPin, ArrowRight, LogOut,
    Stethoscope, AlertCircle, CheckCircle2,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { branchApi } from "@/api/branches";
import { parseApiError } from "@/api/client";

// ─────────────────────────────────────────────────────────────────────────────
// Minimal inline UI primitives (avoids importing from ui/ which may rely on
// AppShell context that isn't available during setup)
// ─────────────────────────────────────────────────────────────────────────────

function InputField({
    label,
    required,
    error,
    ...props
}: React.InputHTMLAttributes<HTMLInputElement> & {
    label: string;
    required?: boolean;
    error?: string;
}) {
    return (
        <div className="space-y-1">
            <label className="text-sm font-medium text-ink">
                {label}
                {required && <span className="text-red-500 ml-0.5">*</span>}
            </label>
            <input
                {...props}
                className={`w-full h-10 px-3 rounded-xl border text-sm bg-white text-ink
                    placeholder:text-ink-muted outline-none transition-colors
                    focus:ring-2 focus:ring-brand-400 focus:border-brand-400
                    ${error ? "border-red-300 focus:ring-red-300" : "border-slate-200"}
                `}
            />
            {error && <p className="text-xs text-red-500">{error}</p>}
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Add-branch panel (shown when setupState === "needs_branch")
// ─────────────────────────────────────────────────────────────────────────────

interface BranchForm {
    name: string;
    phone: string;
    email: string;
    city: string;
}

const EMPTY_FORM: BranchForm = { name: "", phone: "", email: "", city: "" };

function AddBranchPanel() {
    const { user, markReady, logout } = useAuthStore();
    const navigate = useNavigate();
    const [form, setForm] = useState<BranchForm>(EMPTY_FORM);
    const [errors, setErrors] = useState<Partial<BranchForm>>({});
    const [apiError, setApiError] = useState<string | null>(null);
    const [isSubmitting, setIsSubmitting] = useState(false);

    const set = (field: keyof BranchForm, value: string) => {
        setForm((prev) => ({ ...prev, [field]: value }));
        setErrors((prev) => ({ ...prev, [field]: undefined }));
        setApiError(null);
    };

    const validate = (): boolean => {
        const next: Partial<BranchForm> = {};
        if (!form.name.trim()) next.name = "Branch name is required";
        setErrors(next);
        return Object.keys(next).length === 0;
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!validate()) return;

        setIsSubmitting(true);
        setApiError(null);

        try {
            const branch = await branchApi.create({
                name: form.name.trim(),
                phone: form.phone.trim() || undefined,
                email: form.email.trim() || undefined,
                address: form.city.trim()
                    ? { city: form.city.trim(), country: "Ghana" }
                    : undefined,
            });
            markReady(branch.id);
            navigate("/drugs", { replace: true });
        } catch (err) {
            setApiError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    const handleLogout = async () => {
        await logout();
        navigate("/login", { replace: true });
    };

    return (
        <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, ease: "easeOut" }}
            className="w-full max-w-md mx-auto"
        >
            {/* Header */}
            <div className="text-center mb-8">
                <div className="inline-flex items-center gap-2.5 mb-5">
                    <div className="w-10 h-10 rounded-2xl bg-brand-600 flex items-center justify-center shadow-sm">
                        <Stethoscope className="w-5 h-5 text-white" />
                    </div>
                    <span className="font-display text-2xl font-bold text-ink tracking-tight">
                        Laso
                    </span>
                </div>
                <h1 className="font-display text-3xl font-bold text-ink">
                    One last step
                </h1>
                <p className="text-ink-secondary mt-2 text-sm">
                    Your account is ready, but{" "}
                    <strong className="text-ink">{user?.full_name ?? "your organization"}</strong>{" "}
                    doesn't have any branches yet. Add your first branch to continue.
                </p>
            </div>

            {/* Card */}
            <div className="bg-white rounded-3xl border border-slate-100 shadow-card overflow-hidden">
                {/* Icon row */}
                <div className="px-8 pt-8 pb-5 border-b border-slate-100 flex items-center gap-3">
                    <div className="w-10 h-10 rounded-2xl bg-brand-50 border border-brand-100 flex items-center justify-center flex-shrink-0">
                        <MapPin className="w-5 h-5 text-brand-600" />
                    </div>
                    <div>
                        <p className="text-sm font-semibold text-ink">Add a Branch</p>
                        <p className="text-xs text-ink-muted">
                            You can add more branches later from Settings
                        </p>
                    </div>
                </div>

                <form onSubmit={handleSubmit} className="px-8 py-6 space-y-4">
                    {apiError && (
                        <div className="rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2">
                            <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                            <p className="text-sm text-red-600">{apiError}</p>
                        </div>
                    )}

                    <InputField
                        label="Branch Name"
                        required
                        placeholder="e.g. Main Branch"
                        value={form.name}
                        onChange={(e) => set("name", e.target.value)}
                        error={errors.name}
                    />
                    <div className="grid grid-cols-2 gap-3">
                        <InputField
                            label="Phone"
                            type="tel"
                            placeholder="+233501234567"
                            value={form.phone}
                            onChange={(e) => set("phone", e.target.value)}
                        />
                        <InputField
                            label="City"
                            placeholder="Accra"
                            value={form.city}
                            onChange={(e) => set("city", e.target.value)}
                        />
                    </div>
                    <InputField
                        label="Email"
                        type="email"
                        placeholder="branch@pharmacy.com"
                        value={form.email}
                        onChange={(e) => set("email", e.target.value)}
                    />

                    <button
                        type="submit"
                        disabled={isSubmitting}
                        className="w-full h-11 rounded-xl bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold
                            flex items-center justify-center gap-2 transition-colors
                            disabled:opacity-60 disabled:cursor-not-allowed mt-2"
                    >
                        {isSubmitting ? (
                            <>
                                <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                                Creating…
                            </>
                        ) : (
                            <>
                                <CheckCircle2 className="w-4 h-4" />
                                Create Branch & Continue
                            </>
                        )}
                    </button>
                </form>
            </div>

            {/* Log out escape hatch */}
            <div className="text-center mt-5">
                <button
                    type="button"
                    onClick={handleLogout}
                    className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-red-500 transition-colors"
                >
                    <LogOut className="w-3.5 h-3.5" />
                    Sign out
                </button>
            </div>
        </motion.div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Onboard-redirect panel (shown when setupState === "needs_onboard")
// super_admin lands here after login → we send them to /onboarding
// ─────────────────────────────────────────────────────────────────────────────

function OnboardRedirectPanel() {
    const { user, logout } = useAuthStore();
    const navigate = useNavigate();
    const [loggingOut, setLoggingOut] = useState(false);

    const handleLogout = async () => {
        setLoggingOut(true);
        await logout();
        navigate("/login", { replace: true });
    };

    return (
        <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, ease: "easeOut" }}
            className="w-full max-w-md mx-auto text-center"
        >
            <div className="inline-flex items-center gap-2.5 mb-6">
                <div className="w-10 h-10 rounded-2xl bg-brand-600 flex items-center justify-center shadow-sm">
                    <Stethoscope className="w-5 h-5 text-white" />
                </div>
                <span className="font-display text-2xl font-bold text-ink tracking-tight">
                    Laso
                </span>
            </div>

            <div className="bg-white rounded-3xl border border-slate-100 shadow-card p-8 space-y-5">
                <div className="w-14 h-14 rounded-2xl bg-brand-50 border border-brand-100 flex items-center justify-center mx-auto">
                    <Building2 className="w-7 h-7 text-brand-600" />
                </div>

                <div className="space-y-2">
                    <h2 className="font-display text-2xl font-bold text-ink">
                        Set up an organization
                    </h2>
                    <p className="text-sm text-ink-secondary leading-relaxed">
                        Welcome,{" "}
                        <strong className="text-ink">{user?.full_name ?? "Super Admin"}</strong>!
                        Your account has platform-level access. To get started, create and
                        onboard a new pharmacy organization.
                    </p>
                </div>

                <button
                    type="button"
                    onClick={() => navigate("/onboarding", { replace: true })}
                    className="w-full h-11 rounded-xl bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold
                        flex items-center justify-center gap-2 transition-colors"
                >
                    Start Onboarding
                    <ArrowRight className="w-4 h-4" />
                </button>
            </div>

            <div className="text-center mt-5">
                <button
                    type="button"
                    onClick={handleLogout}
                    disabled={loggingOut}
                    className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-red-500 transition-colors disabled:opacity-40"
                >
                    <LogOut className="w-3.5 h-3.5" />
                    Sign out
                </button>
            </div>
        </motion.div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page shell — picks the right panel based on setupState
// ─────────────────────────────────────────────────────────────────────────────

export default function SetupRequiredPage() {
    const { setupState } = useAuthStore();

    return (
        <div className="min-h-screen bg-gradient-to-br from-brand-50 via-white to-slate-50 flex items-start justify-center px-4 py-16">
            {setupState === "needs_onboard" ? (
                <OnboardRedirectPanel />
            ) : (
                <AddBranchPanel />
            )}
        </div>
    );
}