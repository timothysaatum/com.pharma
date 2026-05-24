import { motion } from "framer-motion";
import { CheckCircle2, Building2, User, MapPin, ClipboardList } from "lucide-react";
import { cn } from "@/lib/utils";
import { STEPS } from "@/hooks/useOnboarding";

const STEP_ICONS = [Building2, User, MapPin, ClipboardList];

interface StepIndicatorProps {
    currentStep: number;
    onStepClick?: (step: number) => void;
    completedSteps: Set<number>;
}

export function StepIndicator({
    currentStep,
    onStepClick,
    completedSteps,
}: StepIndicatorProps) {
    return (
        <nav aria-label="Onboarding progress" className="w-full">
            <ol className="flex items-center">
                {STEPS.map((step, index) => {
                    const Icon = STEP_ICONS[index];
                    const isCompleted = completedSteps.has(index);
                    const isActive = index === currentStep;
                    const isClickable = isCompleted && onStepClick;

                    return (
                        <li key={step.id} className="flex items-center flex-1 last:flex-none">
                            {/* Step node */}
                            <div className="flex flex-col items-center gap-1.5">
                                <button
                                    type="button"
                                    onClick={() => isClickable && onStepClick(index)}
                                    disabled={!isClickable}
                                    aria-current={isActive ? "step" : undefined}
                                    className={cn(
                                        "w-10 h-10 rounded-full flex items-center justify-center transition-all duration-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
                                        isCompleted && "bg-brand-600 text-white",
                                        isActive && !isCompleted && "bg-brand-600 text-white ring-4 ring-brand-100",
                                        !isCompleted && !isActive && "bg-slate-100 text-ink-muted",
                                        isClickable && "cursor-pointer hover:bg-brand-700",
                                        !isClickable && "cursor-default"
                                    )}
                                >
                                    {isCompleted && !isActive ? (
                                        <CheckCircle2 className="w-5 h-5" />
                                    ) : (
                                        <Icon className="w-4 h-4" />
                                    )}
                                </button>

                                <div className="text-center">
                                    <p
                                        className={cn(
                                            "text-xs font-semibold whitespace-nowrap leading-tight",
                                            isActive ? "text-brand-600" : isCompleted ? "text-ink" : "text-ink-muted"
                                        )}
                                    >
                                        {step.label}
                                    </p>
                                    <p
                                        className={cn(
                                            "text-[10px] whitespace-nowrap hidden sm:block",
                                            isActive || isCompleted ? "text-ink-muted" : "text-slate-300"
                                        )}
                                    >
                                        {step.description}
                                    </p>
                                </div>
                            </div>

                            {/* Connector line */}
                            {index < STEPS.length - 1 && (
                                <div className="flex-1 mx-2 mb-6 relative">
                                    <div className="h-px bg-slate-200 w-full" />
                                    <motion.div
                                        className="absolute top-0 left-0 h-px bg-brand-400"
                                        initial={{ width: "0%" }}
                                        animate={{
                                            width: completedSteps.has(index) ? "100%" : "0%",
                                        }}
                                        transition={{ duration: 0.4, ease: "easeInOut" }}
                                    />
                                </div>
                            )}
                        </li>
                    );
                })}
            </ol>
        </nav>
    );
}