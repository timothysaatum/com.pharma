/**
 * DrugCategoryModal.tsx
 * =====================
 * Modal for creating and editing drug categories.
 *
 * Supports:
 *   - Creating a root category (no parent)
 *   - Creating a sub-category under an existing parent
 *   - Editing an existing category's name, description, or parent
 *
 * After a successful create or update, the caller's `onSuccess` callback
 * receives the saved category.  The caller is responsible for calling
 * `useCategoryTree().invalidate()` so the tree cache refreshes everywhere.
 *
 * Endpoints used:
 *   POST  /drugs/categories          (create)
 *   PATCH /drugs/categories/{id}     (update)
 *
 * Permissions: manage_drugs  (admin, manager, super_admin)
 */

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import type { Resolver } from "react-hook-form";
import { motion } from "framer-motion";
import { X, FolderTree, AlertCircle, ChevronDown } from "lucide-react";
import { drugApi } from "@/api/drugs";
import { useAuthStore } from "@/stores/authStore";
import { parseApiError } from "@/api/client";
import type { DrugCategory, DrugCategoryTree } from "@/types";
import { useState } from "react";

// ── Schema ────────────────────────────────────────────────────────────────────

const categorySchema = z.object({
    name: z
        .string()
        .min(1, "Category name is required")
        .max(255, "Too long"),
    description: z.string().max(500).optional().or(z.literal("")),
    // Empty string means "no parent" (root category)
    parent_id: z.string().optional().or(z.literal("")),
});

type CategoryFormValues = z.infer<typeof categorySchema>;

// ── Helpers ───────────────────────────────────────────────────────────────────

function clean(v: string | undefined): string | undefined {
    return v === "" ? undefined : v;
}

/**
 * Flatten a category tree into a list of { id, name, level } entries
 * for the parent selector dropdown. Each entry is indented by level
 * so the hierarchy is visually clear without nested <optgroup> elements.
 */
function flattenForSelect(
    nodes: DrugCategoryTree[],
    depth = 0,
    excludeId?: string
): Array<{ id: string; name: string; depth: number }> {
    const result: Array<{ id: string; name: string; depth: number }> = [];
    for (const node of nodes) {
        // Exclude the node being edited and all its descendants to prevent
        // circular parent references (a category cannot be its own ancestor).
        if (node.id === excludeId) continue;
        result.push({ id: node.id, name: node.name, depth });
        if (node.children?.length) {
            result.push(...flattenForSelect(node.children, depth + 1, excludeId));
        }
    }
    return result;
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface DrugCategoryModalProps {
    /** Existing category to edit. Undefined = create mode. */
    category?: DrugCategory;
    /** Pre-select a parent when opening the modal from a child-add action. */
    defaultParentId?: string;
    /** Full category tree for the parent selector. */
    categoryTree: DrugCategoryTree[];
    onSuccess: (saved: DrugCategory) => void;
    onCancel: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function DrugCategoryModal({
    category,
    defaultParentId,
    categoryTree,
    onSuccess,
    onCancel,
}: DrugCategoryModalProps) {
    const { user } = useAuthStore();
    const isEdit = !!category;

    const [isSubmitting, setIsSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const {
        register,
        handleSubmit,
        formState: { errors },
        reset,
    } = useForm<CategoryFormValues>({
        resolver: zodResolver(categorySchema) as Resolver<CategoryFormValues>,
        defaultValues: {
            name: category?.name ?? "",
            description: category?.description ?? "",
            parent_id: category?.parent_id ?? defaultParentId ?? "",
        },
    });

    // Re-populate when switching from create → edit (e.g. modal reuse)
    useEffect(() => {
        reset({
            name: category?.name ?? "",
            description: category?.description ?? "",
            parent_id: category?.parent_id ?? defaultParentId ?? "",
        });
    }, [category, defaultParentId, reset]);

    const onSubmit = async (values: CategoryFormValues) => {
        if (!user?.organization_id) {
            setError("Unable to determine your organisation. Please reload.");
            return;
        }

        setIsSubmitting(true);
        setError(null);

        try {
            let saved: DrugCategory;

            if (isEdit) {
                // PATCH /drugs/categories/{id}
                saved = await drugApi.updateCategory(category.id, {
                    name: values.name,
                    description: clean(values.description) ?? null,
                    parent_id: clean(values.parent_id) ?? null,
                });
            } else {
                // POST /drugs/categories
                saved = await drugApi.createCategory({
                    name: values.name,
                    description: clean(values.description),
                    parent_id: clean(values.parent_id),
                    organization_id: user.organization_id,
                });
            }

            onSuccess(saved);
        } catch (err) {
            setError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    // Build the flat list for the parent selector, excluding the category
    // being edited (and its descendants) to prevent circular references.
    const parentOptions = flattenForSelect(categoryTree, 0, category?.id);

    const inputCls =
        "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink " +
        "focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white";
    const labelCls = "block text-sm font-medium text-ink mb-1.5";

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm">
            <motion.div
                initial={{ opacity: 0, scale: 0.97, y: 12 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.97, y: 8 }}
                transition={{ duration: 0.18 }}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-md flex flex-col"
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-brand-50 flex items-center justify-center">
                            <FolderTree className="w-5 h-5 text-brand-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">
                                {isEdit ? "Edit Category" : "New Category"}
                            </h2>
                            <p className="text-xs text-ink-muted">
                                {isEdit
                                    ? `Editing "${category.name}"`
                                    : "Add a drug category to your formulary"}
                            </p>
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={onCancel}
                        className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Error banner */}
                {error && (
                    <div className="mx-6 mt-4 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2">
                        <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                        <p className="text-sm text-red-600">{error}</p>
                    </div>
                )}

                {/* Form */}
                <form onSubmit={handleSubmit(onSubmit)} className="px-6 py-5 space-y-4">
                    {/* Name */}
                    <div>
                        <label className={labelCls}>
                            Category Name <span className="text-red-500">*</span>
                        </label>
                        <input
                            {...register("name")}
                            placeholder="e.g. Antibiotics, Analgesics, Vitamins"
                            className={inputCls}
                            autoFocus
                        />
                        {errors.name && (
                            <p className="mt-1 text-xs text-red-500">{errors.name.message}</p>
                        )}
                    </div>

                    {/* Description */}
                    <div>
                        <label className={labelCls}>
                            Description{" "}
                            <span className="font-normal text-ink-muted">(optional)</span>
                        </label>
                        <textarea
                            {...register("description")}
                            rows={2}
                            placeholder="Brief description of this category…"
                            className="w-full px-3 py-2 rounded-xl border border-slate-200 text-sm text-ink resize-none focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500"
                        />
                        {errors.description && (
                            <p className="mt-1 text-xs text-red-500">{errors.description.message}</p>
                        )}
                    </div>

                    {/* Parent category */}
                    <div>
                        <label className={labelCls}>
                            Parent Category{" "}
                            <span className="font-normal text-ink-muted">(optional — leave blank for root)</span>
                        </label>
                        <div className="relative">
                            <select
                                {...register("parent_id")}
                                className={`${inputCls} appearance-none pr-8`}
                            >
                                <option value="">— None (root category) —</option>
                                {parentOptions.map((opt) => (
                                    <option key={opt.id} value={opt.id}>
                                        {"\u00A0\u00A0".repeat(opt.depth * 2)}
                                        {opt.depth > 0 ? "↳ " : ""}
                                        {opt.name}
                                    </option>
                                ))}
                            </select>
                            <ChevronDown className="absolute right-3 top-3 w-4 h-4 text-ink-muted pointer-events-none" />
                        </div>

                        {/* Visual hint showing resulting hierarchy */}
                        {isEdit && category.path && (
                            <p className="mt-1.5 text-xs text-ink-muted font-mono">
                                Current path: {category.path}
                            </p>
                        )}
                    </div>

                    {/* Footer buttons */}
                    <div className="flex justify-end gap-2 pt-2">
                        <button
                            type="button"
                            onClick={onCancel}
                            className="px-4 py-2 text-sm font-medium text-ink-secondary hover:text-ink hover:bg-slate-100 rounded-xl transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={isSubmitting}
                            className="px-5 py-2 text-sm font-semibold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors disabled:opacity-60 flex items-center gap-2"
                        >
                            {isSubmitting && (
                                <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                </svg>
                            )}
                            {isSubmitting
                                ? isEdit ? "Saving…" : "Creating…"
                                : isEdit ? "Save Changes" : "Create Category"}
                        </button>
                    </div>
                </form>
            </motion.div>
        </div>
    );
}