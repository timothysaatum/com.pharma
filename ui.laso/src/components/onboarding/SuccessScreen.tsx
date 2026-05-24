import { motion } from "framer-motion";
import { CheckCircle2, ArrowRight, Copy, Check, MapPin } from "lucide-react";
import { useState } from "react";
import type { OnboardingResponse } from "@/types";
import { Badge, Button } from "../ui";

interface SuccessScreenProps {
    result: OnboardingResponse;
    onGoToLogin: () => void;
}

export function SuccessScreen({ result, onGoToLogin }: SuccessScreenProps) {
    const [copied, setCopied] = useState(false);

    const copyCredentials = () => {
        const text = `Username: ${result.admin_user.username}\nOrganization: ${result.organization.name}`;
        navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        });
    };

    return (
        <motion.div
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.35, ease: "easeOut" }}
            className="flex flex-col items-center text-center py-4 gap-6"
        >
            {/* Success icon */}
            <motion.div
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                transition={{ delay: 0.15, type: "spring", stiffness: 200, damping: 15 }}
                className="w-20 h-20 rounded-full bg-brand-50 border-4 border-brand-100 flex items-center justify-center"
            >
                <CheckCircle2 className="w-10 h-10 text-brand-600" />
            </motion.div>

            {/* Message */}
            <div className="space-y-2">
                <h2 className="font-display text-3xl font-bold text-ink">
                    You're all set!
                </h2>
                <p className="text-ink-secondary max-w-sm leading-relaxed">
                    <strong className="text-ink">{result.organization.name}</strong> has
                    been created successfully with{" "}
                    <strong className="text-ink">
                        {result.branches.length} branch{result.branches.length !== 1 ? "es" : ""}
                    </strong>
                    .
                </p>
            </div>

            {/* Summary pills */}
            <div className="flex flex-wrap gap-2 justify-center">
                <Badge variant="success">✓ Organization created</Badge>
                <Badge variant="success">✓ Admin account ready</Badge>
                <Badge variant="success">
                    ✓ {result.branches.length} branch{result.branches.length !== 1 ? "es" : ""} set up
                </Badge>
            </div>

            {/* FIX: List branch names and codes so admin can share them with staff.
                Previously only showed the count, leaving admins with no actionable info. */}
            {result.branches.length > 0 && (
                <div className="w-full max-w-sm rounded-2xl bg-slate-50 border border-slate-200 overflow-hidden text-left">
                    <div className="px-4 py-3 border-b border-slate-200 flex items-center gap-2">
                        <MapPin className="w-4 h-4 text-brand-600" />
                        <p className="text-sm font-semibold text-ink">Created branches</p>
                    </div>
                    <div className="divide-y divide-slate-100">
                        {result.branches.map((branch) => (
                            <div key={branch.id} className="px-4 py-3 flex items-center justify-between gap-3">
                                <span className="text-sm text-ink font-medium truncate">
                                    {branch.name}
                                </span>
                                <span className="text-xs font-mono text-brand-700 bg-brand-50 px-2 py-0.5 rounded flex-shrink-0">
                                    {branch.code}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Credentials card */}
            <div className="w-full max-w-sm rounded-2xl bg-slate-50 border border-slate-200 p-5 text-left space-y-3">
                <div className="flex items-center justify-between">
                    <p className="text-sm font-semibold text-ink">Your login credentials</p>
                    <button
                        type="button"
                        onClick={copyCredentials}
                        className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 font-medium"
                    >
                        {copied ? (
                            <><Check className="w-3 h-3" /> Copied</>
                        ) : (
                            <><Copy className="w-3 h-3" /> Copy</>
                        )}
                    </button>
                </div>
                <div className="space-y-2">
                    <div className="flex items-center justify-between">
                        <span className="text-xs text-ink-muted">Username</span>
                        <span className="text-sm font-mono font-semibold text-ink">
                            {result.admin_user.username}
                        </span>
                    </div>
                    <div className="flex items-center justify-between">
                        <span className="text-xs text-ink-muted">Organization</span>
                        <span className="text-sm font-medium text-ink">
                            {result.organization.name}
                        </span>
                    </div>
                    {result.temp_credentials?.note && (
                        <div className="pt-2 border-t border-slate-200">
                            <p className="text-xs text-amber-600 font-medium">
                                ⚠ {result.temp_credentials.note}
                            </p>
                        </div>
                    )}
                </div>
            </div>

            <Button onClick={onGoToLogin} size="lg" className="w-full max-w-sm">
                Go to Login
                <ArrowRight className="w-4 h-4" />
            </Button>

            <p className="text-xs text-ink-muted">
                Save your credentials and branch codes before proceeding
            </p>
        </motion.div>
    );
}