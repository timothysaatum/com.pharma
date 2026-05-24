import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import type { Resolver } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";

export const loginSchema = z.object({
    username: z
        .string()
        .min(3, "Username must be at least 3 characters")
        .max(100),
    password: z
        .string()
        .min(8, "Password must be at least 8 characters")
        .max(100),
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

    const form = useForm<LoginValues>({
        resolver: zodResolver(loginSchema) as Resolver<LoginValues>,
        mode: "onTouched",
        defaultValues: { username: "", password: "" },
    });

    // Clear error whenever the user edits any field
    form.watch(() => {
        if (error) setError(null);
    });

    const submit = async (values: LoginValues) => {
        setIsSubmitting(true);
        setError(null);
        setIsLocked(false);

        try {
            await login(values.username, values.password);

            if (options.onSuccess) {
                // Caller handles all navigation
                await options.onSuccess();
                return;
            }

            // Default: navigate based on setupState set by authStore.login()
            const state = useAuthStore.getState().setupState;

            switch (state) {
                case "ready":
                    navigate("/drugs", { replace: true });
                    break;
                case "needs_branch":
                    navigate("/setup", { replace: true });
                    break;
                case "needs_onboard":
                    navigate("/onboarding", { replace: true });
                    break;
                default:
                    navigate("/drugs", { replace: true });
            }
        } catch (err) {
            const message = parseApiError(err);

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
        submit,
        clearError: () => setError(null),
    };
}