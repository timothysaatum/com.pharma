import { useState, useCallback, useRef } from "react";
import { UseFormReturn } from "react-hook-form";
import { organizationApi, buildOnboardingPayload } from "@/api/organization";
import { parseApiError } from "@/api/client";
import type { OnboardingValues } from "@/lib/validators";
import type { OnboardingResponse } from "@/types";

export const STEPS = [
    { id: 0, key: "org", label: "Organization", description: "Basic details" },
    { id: 1, key: "admin", label: "Admin Account", description: "Access credentials" },
    { id: 2, key: "branches", label: "Branches", description: "Locations" },
    { id: 3, key: "review", label: "Review", description: "Confirm & submit" },
] as const;

export type StepKey = (typeof STEPS)[number]["key"];

// Fields validated per step
const STEP_FIELDS: (keyof OnboardingValues)[][] = [
    ["name", "type", "subscription_tier", "currency", "timezone"],
    ["username", "admin_email", "full_name", "password", "confirm_password"],
    ["branches"],
    [], // review step — no new fields
];

interface UseOnboardingOptions {
    form: UseFormReturn<OnboardingValues>;
    onSuccess: (result: OnboardingResponse) => void;
}

export function useOnboarding({ form, onSuccess }: UseOnboardingOptions) {
    const [currentStep, setCurrentStep] = useState(0);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [submitError, setSubmitError] = useState<string | null>(null);
    const [retryCount, setRetryCount] = useState(0);
    const submitAttemptRef = useRef(0);

    const isFirstStep = currentStep === 0;
    const isLastStep = currentStep === STEPS.length - 1;
    const isReviewStep = currentStep === 3;

    const goToStep = useCallback((step: number) => {
        setCurrentStep(step);
        setSubmitError(null);
    }, []);

    const nextStep = useCallback(async () => {
        // Validate only the current step's fields
        const fieldsToValidate = STEP_FIELDS[currentStep] as (keyof OnboardingValues)[];
        const valid = fieldsToValidate.length === 0
            ? true
            : await form.trigger(fieldsToValidate);

        if (!valid) return false;

        setSubmitError(null);
        setCurrentStep((s) => Math.min(s + 1, STEPS.length - 1));
        return true;
    }, [currentStep, form]);

    const prevStep = useCallback(() => {
        setSubmitError(null);
        setCurrentStep((s) => Math.max(s - 1, 0));
    }, []);

    const submit = useCallback(
        async (values: OnboardingValues) => {
            const attempt = ++submitAttemptRef.current;
            setIsSubmitting(true);
            setSubmitError(null);

            try {
                const payload = buildOnboardingPayload(values);

                // Guard against stale submissions (e.g. double-click)
                if (attempt !== submitAttemptRef.current) return;

                const result = await organizationApi.onboard(payload);
                onSuccess(result);
            } catch (err) {
                if (attempt !== submitAttemptRef.current) return;
                setSubmitError(parseApiError(err));
                setRetryCount((c) => c + 1);
            } finally {
                if (attempt === submitAttemptRef.current) {
                    setIsSubmitting(false);
                }
            }
        },
        [onSuccess]
    );

    const clearError = useCallback(() => setSubmitError(null), []);

    return {
        currentStep,
        isFirstStep,
        isLastStep,
        isReviewStep,
        isSubmitting,
        submitError,
        retryCount,
        goToStep,
        nextStep,
        prevStep,
        submit,
        clearError,
    };
}