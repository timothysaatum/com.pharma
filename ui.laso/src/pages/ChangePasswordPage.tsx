import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { KeyRound, Eye, EyeOff, CheckCircle, AlertTriangle, Loader2 } from "lucide-react";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";

export default function ChangePasswordPage() {
    const navigate = useNavigate();
    const { clearPasswordChangeRequired, logout } = useAuthStore();

    const [newPassword, setNewPassword] = useState("");
    const [confirmPassword, setConfirmPassword] = useState("");
    const [showPassword, setShowPassword] = useState(false);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);

        if (newPassword.length < 8) {
            setError("Password must be at least 8 characters");
            return;
        }
        if (newPassword !== confirmPassword) {
            setError("Passwords do not match");
            return;
        }

        setIsSubmitting(true);
        try {
            await authApi.forceChangePassword({ new_password: newPassword });
            setSuccess(true);

            // Re-fetch user to get updated password_change_required flag
            await clearPasswordChangeRequired();

            // Redirect to home after a brief moment
            setTimeout(() => navigate("/", { replace: true }), 1500);
        } catch (err: unknown) {
            setError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    if (success) {
        return (
            <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
                <motion.div
                    initial={{ opacity: 0, scale: 0.95 }}
                    animate={{ opacity: 1, scale: 1 }}
                    className="w-full max-w-sm bg-white rounded-2xl shadow-xl border border-slate-100 p-8 text-center"
                >
                    <div className="w-14 h-14 rounded-full bg-green-50 flex items-center justify-center mx-auto mb-4">
                        <CheckCircle className="w-7 h-7 text-green-600" />
                    </div>
                    <h2 className="font-display text-lg font-bold text-ink mb-2">Password Changed</h2>
                    <p className="text-sm text-ink-muted mb-6">
                        Your password has been updated successfully. Redirecting you to the application…
                    </p>
                    <Loader2 className="w-5 h-5 animate-spin text-brand-500 mx-auto" />
                </motion.div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
            <motion.div
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                className="w-full max-w-sm"
            >
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 overflow-hidden">
                    {/* Header */}
                    <div className="bg-brand-600 px-6 py-6 text-center">
                        <div className="w-12 h-12 rounded-full bg-white/20 flex items-center justify-center mx-auto mb-3">
                            <KeyRound className="w-6 h-6 text-white" />
                        </div>
                        <h1 className="font-display text-lg font-bold text-white">
                            Change Your Password
                        </h1>
                        <p className="text-sm text-white/80 mt-1">
                            You must set a new password before accessing the application.
                        </p>
                    </div>

                    {/* Form */}
                    <form onSubmit={handleSubmit} className="px-6 py-6 space-y-5">
                        {error && (
                            <div className="flex items-start gap-2.5 rounded-xl bg-red-50 border border-red-100 px-4 py-3">
                                <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                                <p className="text-xs text-red-700">{error}</p>
                            </div>
                        )}

                        <div>
                            <label className="block text-xs font-semibold text-ink mb-1.5">
                                New Password
                            </label>
                            <div className="relative">
                                <input
                                    type={showPassword ? "text" : "password"}
                                    value={newPassword}
                                    onChange={(e) => setNewPassword(e.target.value)}
                                    placeholder="Min 8 characters"
                                    className="w-full h-10 pl-3 pr-10 rounded-lg border border-slate-200 bg-white text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-brand-500"
                                    autoFocus
                                    disabled={isSubmitting}
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowPassword((v) => !v)}
                                    className="absolute right-3 top-1/2 -translate-y-1/2 text-ink-muted hover:text-ink"
                                    tabIndex={-1}
                                >
                                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                </button>
                            </div>
                            <p className="text-xs text-ink-muted mt-1">
                                Must contain uppercase, lowercase, digit, and special character.
                            </p>
                        </div>

                        <div>
                            <label className="block text-xs font-semibold text-ink mb-1.5">
                                Confirm Password
                            </label>
                            <input
                                type={showPassword ? "text" : "password"}
                                value={confirmPassword}
                                onChange={(e) => setConfirmPassword(e.target.value)}
                                placeholder="Re-enter new password"
                                className="w-full h-10 pl-3 pr-3 rounded-lg border border-slate-200 bg-white text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-brand-500"
                                disabled={isSubmitting}
                            />
                        </div>

                        <button
                            type="submit"
                            disabled={isSubmitting || !newPassword || !confirmPassword}
                            className="w-full py-2.5 text-sm font-bold text-white rounded-xl transition-all
                                bg-brand-600 hover:bg-brand-700 active:scale-[0.99]
                                disabled:opacity-50 disabled:cursor-not-allowed
                                flex items-center justify-center gap-2"
                        >
                            {isSubmitting ? (
                                <>
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                    Updating…
                                </>
                            ) : (
                                "Change Password"
                            )}
                        </button>

                        <div className="text-center">
                            <button
                                type="button"
                                onClick={() => {
                                    logout();
                                    navigate("/login", { replace: true });
                                }}
                                className="text-xs text-ink-muted hover:text-ink underline transition-colors"
                            >
                                Log out and return to login
                            </button>
                        </div>
                    </form>
                </div>
            </motion.div>
        </div>
    );
}
