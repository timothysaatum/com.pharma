import { UseFormReturn, useFieldArray } from "react-hook-form";
import { motion, AnimatePresence } from "framer-motion";
import { MapPin, Plus, Trash2, Phone, Mail, Info } from "lucide-react";
import type { OnboardingValues } from "@/lib/validators";
import { Badge, Input } from "../ui";

interface BranchesStepProps {
    form: UseFormReturn<OnboardingValues>;
}

export function BranchesStep({ form }: BranchesStepProps) {
    const { register, control, formState: { errors } } = form;
    const { fields, append, remove } = useFieldArray({
        control,
        name: "branches",
    });

    const branchErrors = errors.branches;

    return (
        <motion.div
            key="branches-step"
            initial={{ opacity: 0, x: 32 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -32 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            className="space-y-5"
        >
            <div className="flex items-start justify-between">
                <div>
                    <h2 className="font-display text-2xl font-bold text-ink tracking-tight">
                        Branches
                    </h2>
                    <p className="text-sm text-ink-secondary mt-1">
                        Add your pharmacy locations. Skip to create a default branch automatically.
                    </p>
                </div>
                <Badge variant={fields.length > 0 ? "success" : "default"}>
                    {fields.length} / 10
                </Badge>
            </div>

            {/* Root-level branch duplicate error */}
            {branchErrors?.root?.message && (
                <div className="rounded-xl bg-red-50 border border-red-100 p-3">
                    <p className="text-sm text-red-600">{branchErrors.root.message}</p>
                </div>
            )}

            {/* Branch cards */}
            <div className="space-y-3">
                <AnimatePresence initial={false}>
                    {fields.map((field, index) => {
                        const err = branchErrors?.[index];
                        return (
                            <motion.div
                                key={field.id}
                                initial={{ opacity: 0, y: -10, height: 0 }}
                                animate={{ opacity: 1, y: 0, height: "auto" }}
                                exit={{ opacity: 0, y: -10, height: 0 }}
                                transition={{ duration: 0.2 }}
                            >
                                <div className="rounded-2xl border border-slate-200 bg-slate-50/60 p-4 space-y-3">
                                    {/* Branch header */}
                                    <div className="flex items-center justify-between">
                                        <div className="flex items-center gap-2">
                                            <div className="w-7 h-7 rounded-lg bg-brand-600 flex items-center justify-center">
                                                <span className="text-xs font-bold text-white">
                                                    {index + 1}
                                                </span>
                                            </div>
                                            <span className="text-sm font-semibold text-ink">
                                                Branch {index + 1}
                                            </span>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => remove(index)}
                                            className="p-1.5 rounded-lg text-ink-muted hover:text-red-500 hover:bg-red-50 transition-colors"
                                            aria-label={`Remove branch ${index + 1}`}
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>

                                    {/* Branch fields */}
                                    <div className="grid grid-cols-2 gap-3">
                                        <div className="col-span-2">
                                            <Input
                                                placeholder="Branch name *"
                                                required
                                                leftIcon={<MapPin className="w-4 h-4" />}
                                                error={err?.name?.message}
                                                {...register(`branches.${index}.name`)}
                                            />
                                        </div>
                                        <Input
                                            placeholder="Phone"
                                            type="tel"
                                            leftIcon={<Phone className="w-3.5 h-3.5" />}
                                            error={err?.branch_phone?.message}
                                            {...register(`branches.${index}.branch_phone`)}
                                        />
                                        <Input
                                            placeholder="Email"
                                            type="email"
                                            leftIcon={<Mail className="w-3.5 h-3.5" />}
                                            error={err?.branch_email?.message}
                                            {...register(`branches.${index}.branch_email`)}
                                        />
                                        <Input
                                            placeholder="Street address"
                                            {...register(`branches.${index}.address.street`)}
                                        />
                                        <Input
                                            placeholder="City"
                                            {...register(`branches.${index}.address.city`)}
                                        />
                                    </div>
                                </div>
                            </motion.div>
                        );
                    })}
                </AnimatePresence>

                {/* Add branch button */}
                {fields.length < 10 && (
                    <button
                        type="button"
                        onClick={() =>
                            append({
                                name: "",
                                branch_phone: "",
                                branch_email: "",
                                address: { street: "", city: "", country: "Ghana" },
                            })
                        }
                        className="w-full h-12 rounded-2xl border-2 border-dashed border-slate-200 flex items-center justify-center gap-2 text-sm text-ink-muted hover:border-brand-400 hover:text-brand-600 hover:bg-brand-50 transition-all duration-150"
                    >
                        <Plus className="w-4 h-4" />
                        Add a branch
                    </button>
                )}
            </div>

            {/* Info note */}
            {fields.length === 0 && (
                <div className="rounded-xl bg-amber-50 border border-amber-100 p-4 flex gap-3">
                    <Info className="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-amber-700">
                        No branches added — a <strong>Main Branch</strong> will be created
                        automatically. You can add more branches after setup from the
                        Branches section.
                    </p>
                </div>
            )}
        </motion.div>
    );
}