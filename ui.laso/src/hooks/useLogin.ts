import { useState, useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import type { Resolver } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";
import { getHomePath } from "@/lib/routes";

export const loginSchema = z.object({
    username: z
        .string()
        .min(3, "Username must be at least 3 characters")
        .max(100),
    password: z
        .string()
        .min(8, "Password must be at least 8 characters")
        .max(100),
    totp_code: z
        .string()
        .trim()
        .optional(),
});

export type LoginValues = z.infer<typeof loginSchema>;

// ─────────────────────────────────────────────────────────────────────────────
// Options
// ─────────────────────────────────────────────────────────────────────────────

export interface UseLoginOptions {
    /**
     * If provided, this callback is called after a successful login INSTEAD of
     * the default setupState-based navigation.  Use this when the caller needs
     * to perform extra work (e.g. fetching branches) before deciding where to
     * redirect.
     */
    onSuccess?: () => void | Promise<void>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Hook
// ─────────────────────────────────────────────────────────────────────────────

export function useLogin(options: UseLoginOptions = {}) {
    const { login } = useAuthStore();
    const navigate = useNavigate();
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [isLocked, setIsLocked] = useState(false);
    const [requiresMfa, setRequiresMfa] = useState(false);

    const form = useForm<LoginValues>({
        resolver: zodResolver(loginSchema) as Resolver<LoginValues>,
        mode: "onTouched",
        defaultValues: { username: "", password: "", totp_code: "" },
    });

    // Clear error whenever the user edits any field
    useEffect(() => {
        const sub = form.watch((_value, info) => {
            if (error) setError(null);
            if (
                requiresMfa &&
                (info.name === "username" || info.name === "password")
            ) {
                setRequiresMfa(false);
                form.setValue("totp_code", "");
            }
        });
        return () => sub.unsubscribe();
    }, [form, error, requiresMfa]);

    const submit = async (values: LoginValues) => {
        setIsSubmitting(true);
        setError(null);
        setIsLocked(false);

        try {
            const totpCode = values.totp_code?.replace(/\s+/g, "") ?? "";

            if (requiresMfa && !/^\d{6}$/.test(totpCode)) {
                setError("Enter the 6-digit code from your authenticator app.");
                form.setFocus("totp_code");
                return;
            }

            await login(
                values.username,
                values.password,
                requiresMfa ? totpCode : undefined
            );

            if (options.onSuccess) {
                // Caller handles all navigation
                await options.onSuccess();
                return;
            }

            // Default: navigate based on setupState set by authStore.login()
            const { setupState: state, user } = useAuthStore.getState();

            switch (state) {
                case "ready":
                    navigate(getHomePath(user), { replace: true });
                    break;
                case "needs_branch":
                    navigate("/setup", { replace: true });
                    break;
                case "needs_onboard":
                    navigate("/onboarding", { replace: true });
                    break;
                default:
                    navigate(getHomePath(user), { replace: true });
            }
        } catch (err) {
            const message = parseApiError(err);

            if (message === "MFA_REQUIRED") {
                setRequiresMfa(true);
                form.setValue("totp_code", "");
                setError(null);
                setTimeout(() => form.setFocus("totp_code"), 0);
                return;
            }

            if (message.toLowerCase().includes("verification code")) {
                setRequiresMfa(true);
                form.setValue("totp_code", "");
                setError(message);
                setTimeout(() => form.setFocus("totp_code"), 0);
                return;
            }

            if (
                message.toLowerCase().includes("locked") ||
                message.toLowerCase().includes("too many")
            ) {
                setIsLocked(true);
            }

            setError(message);
        } finally {
            setIsSubmitting(false);
        }
    };

    return {
        form,
        isSubmitting,
        error,
        isLocked,
        requiresMfa,
        submit,
        clearError: () => setError(null),
    };
}
