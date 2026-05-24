import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import type { Resolver } from "react-hook-form";
import { AnimatePresence, motion } from "framer-motion";
import { Stethoscope, ChevronLeft, ChevronRight, CheckCircle2 } from "lucide-react";

import { onboardingSchema, type OnboardingValues } from "@/lib/validators";
import { useOnboarding, STEPS } from "@/hooks/useOnboarding";
import { StepIndicator } from "@/components/onboarding/StepIndicator";
import { OrgStep } from "@/components/onboarding/OrgStep";
import { AdminStep } from "@/components/onboarding/AdminStep";
import { BranchesStep } from "@/components/onboarding/BranchesStep";
import { ReviewStep } from "@/components/onboarding/ReviewStep";
import { SuccessScreen } from "@/components/onboarding/SuccessScreen";
import { Button } from "@/components/ui";
import type { OnboardingResponse } from "@/types";

export default function OnboardingPage() {
    const navigate = useNavigate();
    const [successResult, setSuccessResult] = useState<OnboardingResponse | null>(null);
    const [completedSteps, setCompletedSteps] = useState<Set<number>>(new Set());

    const form = useForm<OnboardingValues>({
        // Cast resolver so react-hook-form uses OnboardingValues as both
        // input and output type. Without this, Zod's .default() fields
        // (subscription_tier, timezone, currency) become optional in the
        // inferred input type, causing a mismatch with UseFormReturn<OnboardingValues>.
        resolver: zodResolver(onboardingSchema) as Resolver<OnboardingValues>,
        mode: "onTouched",
        defaultValues: {
            type: "pharmacy",
            subscription_tier: "basic",
            currency: "GHS",
            timezone: "Africa/Accra",
            branches: [],
            address: { country: "Ghana" },
        },
    });

    const {
        currentStep,
        isFirstStep,
        isLastStep,
        isSubmitting,
        submitError,
        goToStep,
        nextStep,
        prevStep,
        submit,
        clearError,
    } = useOnboarding({
        form,
        onSuccess: (result) => setSuccessResult(result),
    });

    // Mark steps completed when advancing forward
    useEffect(() => {
        setCompletedSteps((prev) => {
            const next = new Set(prev);
            if (currentStep > 0) next.add(currentStep - 1);
            return next;
        });
    }, [currentStep]);

    // Clear the submission error whenever the user edits a field
    useEffect(() => {
        const sub = form.watch(() => {
            if (submitError) clearError();
        });
        return () => sub.unsubscribe();
    }, [form, submitError, clearError]);

    if (successResult) {
        return (
            <PageShell>
                <div className="bg-white rounded-3xl border border-slate-100 shadow-card p-8 w-full max-w-lg mx-auto">
                    <SuccessScreen
                        result={successResult}
                        onGoToLogin={() => navigate("/login")}
                    />
                </div>
            </PageShell>
        );
    }

    return (
        <PageShell>
            <div className="w-full max-w-2xl mx-auto">
                {/* Header */}
                <motion.div
                    initial={{ opacity: 0, y: -16 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4 }}
                    className="text-center mb-8"
                >
                    <div className="inline-flex items-center gap-2.5 mb-5">
                        <div className="w-10 h-10 rounded-2xl bg-brand-600 flex items-center justify-center shadow-sm">
                            <Stethoscope className="w-5 h-5 text-white" />
                        </div>
                        <span className="font-display text-2xl font-bold text-ink tracking-tight">
                            Laso
                        </span>
                    </div>
                    <h1 className="font-display text-4xl font-bold text-ink leading-tight">
                        Set up your pharmacy
                    </h1>
                    <p className="text-ink-secondary mt-2 text-base">
                        Complete setup in 4 steps — takes about 3 minutes
                    </p>
                </motion.div>

                {/* Card */}
                <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4, delay: 0.1 }}
                    className="bg-white rounded-3xl border border-slate-100 shadow-card overflow-hidden"
                >
                    {/* Step indicator */}
                    <div className="px-8 pt-8 pb-6 border-b border-slate-100">
                        <StepIndicator
                            currentStep={currentStep}
                            completedSteps={completedSteps}
                            onStepClick={(step) => {
                                if (completedSteps.has(step)) goToStep(step);
                            }}
                        />
                    </div>

                    {/* Form */}
                    <form
                        onSubmit={form.handleSubmit(submit)}
                        noValidate
                        className="px-8 py-8"
                    >
                        <div className="min-h-[420px]">
                            <AnimatePresence mode="wait">
                                {currentStep === 0 && <OrgStep key="org" form={form} />}
                                {currentStep === 1 && <AdminStep key="admin" form={form} />}
                                {currentStep === 2 && <BranchesStep key="branches" form={form} />}
                                {currentStep === 3 && (
                                    <ReviewStep
                                        key="review"
                                        form={form}
                                        onGoToStep={goToStep}
                                        submitError={submitError}
                                    />
                                )}
                            </AnimatePresence>
                        </div>

                        {/* Footer navigation */}
                        <div className="flex items-center justify-between mt-8 pt-6 border-t border-slate-100">
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={prevStep}
                                disabled={isFirstStep || isSubmitting}
                                className={isFirstStep ? "invisible" : ""}
                            >
                                <ChevronLeft className="w-4 h-4" />
                                Back
                            </Button>

                            <span className="text-xs text-ink-muted font-medium">
                                Step {currentStep + 1} of {STEPS.length}
                            </span>

                            {isLastStep ? (
                                <Button
                                    type="submit"
                                    size="lg"
                                    loading={isSubmitting}
                                    disabled={isSubmitting}
                                >
                                    <CheckCircle2 className="w-4 h-4" />
                                    {isSubmitting ? "Creating…" : "Create Organization"}
                                </Button>
                            ) : (
                                <Button
                                    type="button"
                                    size="lg"
                                    onClick={nextStep}
                                    disabled={isSubmitting}
                                >
                                    Continue
                                    <ChevronRight className="w-4 h-4" />
                                </Button>
                            )}
                        </div>
                    </form>
                </motion.div>

                <p className="text-center text-sm text-ink-muted mt-6">
                    Already have an account?{" "}
                    <a
                        href="/login"
                        className="text-brand-600 hover:text-brand-700 font-semibold hover:underline"
                    >
                        Sign in
                    </a>
                </p>
            </div>
        </PageShell>
    );
}

function PageShell({ children }: { children: React.ReactNode }) {
    return (
        <div className="min-h-screen bg-gradient-to-br from-brand-50 via-white to-slate-50 flex items-start justify-center px-4 py-12">
            {children}
        </div>
    );
}