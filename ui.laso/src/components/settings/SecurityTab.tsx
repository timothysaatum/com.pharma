import { useState } from "react";
import { ShieldCheck, Smartphone, Key, CheckCircle2, AlertCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { Button, Input } from "@/components/ui";
import { parseApiError } from "@/api/client";

export function SecurityTab() {
    const { user, setUser } = useAuthStore();
    const [isLoading, setIsLoading] = useState(false);
    const [secret, setSecret] = useState<string | null>(null);
    const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
    const [totpCode, setTotpCode] = useState("");
    const [password, setPassword] = useState("");

    const isMfaEnabled = user?.two_factor_enabled ?? false;

    const handleSetup = async () => {
        setIsLoading(true);
        try {
            const data = await authApi.mfaSetup();
            setSecret(data.secret);
            setProvisioningUri(data.provisioning_uri);
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsLoading(false);
        }
    };

    const handleVerify = async (e: React.FormEvent) => {
        e.preventDefault();
        if (totpCode.length !== 6) {
            toast.error("Please enter a 6-digit code from your authenticator app.");
            return;
        }
        setIsLoading(true);
        try {
            const updatedUser = await authApi.mfaVerify({ totp_code: totpCode });
            setUser(updatedUser);
            setSecret(null);
            setProvisioningUri(null);
            setTotpCode("");
            toast.success("MFA enabled successfully.");
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsLoading(false);
        }
    };

    const handleDisable = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!password) {
            toast.error("Please enter your password.");
            return;
        }
        setIsLoading(true);
        try {
            const updatedUser = await authApi.mfaDisable({ password });
            setUser(updatedUser);
            setPassword("");
            toast.success("MFA disabled.");
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div className="p-6 max-w-4xl">
            <div className="flex items-center gap-3 mb-6">
                <ShieldCheck className="w-6 h-6 text-brand-600" />
                <div>
                    <h2 className="text-lg font-bold text-ink">Security</h2>
                    <p className="text-sm text-ink-muted">Manage multi-factor authentication for your account.</p>
                </div>
            </div>

            <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
                <div className="p-6 border-b border-slate-100">
                    <div className="flex items-start gap-4">
                        <div className="w-12 h-12 rounded-xl bg-brand-50 flex items-center justify-center flex-shrink-0">
                            <Smartphone className="w-6 h-6 text-brand-600" />
                        </div>
                        <div className="flex-1">
                            <h3 className="font-bold text-ink">Multi-Factor Authentication (MFA)</h3>
                            <p className="text-sm text-ink-muted mt-1">
                                Add an extra layer of security by requiring a one-time code from your authenticator app
                                when you log in.
                            </p>
                        </div>
                        <div className="flex-shrink-0">
                            {isMfaEnabled ? (
                                <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full bg-green-50 text-green-700 text-xs font-bold border border-green-200">
                                    <CheckCircle2 className="w-3.5 h-3.5" />
                                    Enabled
                                </span>
                            ) : (
                                <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full bg-amber-50 text-amber-700 text-xs font-bold border border-amber-200">
                                    <AlertCircle className="w-3.5 h-3.5" />
                                    Disabled
                                </span>
                            )}
                        </div>
                    </div>
                </div>

                {isMfaEnabled ? (
                    <form onSubmit={handleDisable} className="p-6 space-y-4">
                        <label className="block text-sm font-medium text-ink">Disable MFA</label>
                        <p className="text-xs text-ink-muted mb-2">
                            Enter your password to disable MFA. This reduces your account security.
                        </p>
                        <div className="max-w-xs">
                            <Input
                                type="password"
                                placeholder="Enter your password"
                                value={password}
                                onChange={e => setPassword(e.target.value)}
                            />
                        </div>
                        <Button type="submit" variant="secondary" loading={isLoading} disabled={isLoading}>
                            Disable MFA
                        </Button>
                    </form>
                ) : secret ? (
                    <div className="p-6 space-y-5">
                        <div className="p-4 bg-slate-50 rounded-xl border border-slate-200">
                            <label className="block text-xs font-bold text-ink-muted uppercase tracking-wider mb-2">
                                Secret Key
                            </label>
                            <div className="flex items-center gap-2">
                                <code className="flex-1 px-3 py-2 bg-white border border-slate-200 rounded-lg text-sm font-mono text-ink select-all">
                                    {secret}
                                </code>
                                <button
                                    type="button"
                                    onClick={() => { navigator.clipboard.writeText(secret!); toast.success("Secret copied"); }}
                                    className="px-3 py-2 text-xs font-semibold text-brand-600 hover:text-brand-700 hover:bg-brand-50 rounded-lg transition-colors border border-brand-200"
                                >
                                    Copy
                                </button>
                            </div>
                        </div>

                        {provisioningUri && (
                            <div className="p-4 bg-slate-50 rounded-xl border border-slate-200">
                                <label className="block text-xs font-bold text-ink-muted uppercase tracking-wider mb-2">
                                    App Link
                                </label>
                                <p className="text-xs text-ink-muted mb-2">
                                    Copy this link into your authenticator app, or manually enter the secret key above.
                                </p>
                                <code className="block px-3 py-2 bg-white border border-slate-200 rounded-lg text-xs font-mono text-ink break-all select-all">
                                    {provisioningUri}
                                </code>
                                <button
                                    type="button"
                                    onClick={() => { navigator.clipboard.writeText(provisioningUri!); toast.success("Link copied"); }}
                                    className="mt-2 px-3 py-1.5 text-xs font-semibold text-brand-600 hover:text-brand-700 hover:bg-brand-50 rounded-lg transition-colors border border-brand-200"
                                >
                                    Copy URI
                                </button>
                            </div>
                        )}

                        <form onSubmit={handleVerify} className="space-y-4">
                            <div className="max-w-xs">
                                <Input
                                    label="Verification Code"
                                    placeholder="Enter 6-digit code from app"
                                    value={totpCode}
                                    onChange={e => setTotpCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                                />
                            </div>
                            <div className="flex items-center gap-3">
                                <Button type="submit" loading={isLoading} disabled={isLoading}>
                                    <Key className="w-4 h-4" />
                                    Verify & Enable
                                </Button>
                                <Button type="button" variant="ghost" onClick={() => { setSecret(null); setProvisioningUri(null); }}>
                                    Cancel
                                </Button>
                            </div>
                        </form>
                    </div>
                ) : (
                    <div className="p-6">
                        <Button onClick={handleSetup} loading={isLoading} disabled={isLoading}>
                            <ShieldCheck className="w-4 h-4" />
                            Set up MFA
                        </Button>
                    </div>
                )}
            </div>
        </div>
    );
}
