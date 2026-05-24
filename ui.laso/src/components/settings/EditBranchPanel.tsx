/**
 * ─────────────────────────────────────────────────────────────
 * Slide-over panel for editing an existing branch.
 *
 * Pre-fills all fields from the current BranchListItem.
 * Only sends fields that have actually changed (diff before submit).
 * Mirrors BranchUpdate schema exactly — all fields optional.
 *
 * Security: rendered only for admin/super_admin (enforced in BranchesTab
 * and at the server via require_role on PATCH /branches/{id}).
 */

import { useState, useCallback, useEffect } from "react";
import {
    X, Building2, Hash, Phone, Mail,
    AlertCircle, Save,
    ToggleLeft, ToggleRight,
} from "lucide-react";
import type { BranchListItem, BranchUpdate, Branch } from "@/types";

interface Props {
    branch: BranchListItem;
    onSaved: (updated: Branch) => void;
    onClose: () => void;
    onUpdate: (id: string, data: BranchUpdate) => Promise<Branch | null>;
    saving: boolean;
    saveError: string | null;
}

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 " +
    "transition-colors placeholder:text-slate-400";

const labelCls =
    "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

const fieldErrCls = "text-xs text-red-500 mt-1 flex items-center gap-1";

export function EditBranchPanel({ branch, onSaved, onClose, onUpdate, saving, saveError }: Props) {
    // ── Initialise from current branch data ───────────────────
    const [name, setName] = useState(branch.name);
    const [code, setCode] = useState(branch.code);
    const [phone, setPhone] = useState(branch.phone ?? "");
    const [email, setEmail] = useState(branch.email ?? "");
    const [isActive, setIsActive] = useState(branch.is_active);

    // Re-init if the branch prop changes (user clicks a different branch)
    useEffect(() => {
        setName(branch.name);
        setCode(branch.code);
        setPhone(branch.phone ?? "");
        setEmail(branch.email ?? "");
        setIsActive(branch.is_active);
    }, [branch.id]);

    const [errors, setErrors] = useState<Record<string, string>>({});

    // ── Validation ────────────────────────────────────────────
    const validate = (): boolean => {
        const errs: Record<string, string> = {};

        if (!name.trim()) {
            errs.name = "Branch name is required";
        } else if (name.trim().length < 2) {
            errs.name = "Name must be at least 2 characters";
        }

        if (!code.trim()) {
            errs.code = "Branch code is required";
        } else if (!/^[A-Z0-9\-_]+$/i.test(code.trim())) {
            errs.code = "Only letters, numbers, hyphens and underscores";
        }

        if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            errs.email = "Enter a valid email address";
        }

        if (phone) {
            const cleaned = phone.replace(/[\s\-()]/g, "");
            if (!/^\+?[0-9]{10,15}$/.test(cleaned)) {
                errs.phone = "Enter a valid phone number";
            }
        }

        setErrors(errs);
        return Object.keys(errs).length === 0;
    };

    // ── Diff — only send changed fields ──────────────────────
    const buildPatch = (): BranchUpdate => {
        const patch: BranchUpdate = {};
        if (name.trim() !== branch.name) patch.name = name.trim();
        if (code.trim().toUpperCase() !== branch.code) patch.code = code.trim().toUpperCase();
        if ((phone.trim() || null) !== branch.phone) patch.phone = phone.trim() || undefined;
        if ((email.trim() || null) !== branch.email) patch.email = email.trim() || undefined;
        if (isActive !== branch.is_active) patch.is_active = isActive;
        return patch;
    };

    // ── Submit ────────────────────────────────────────────────
    const handleSave = useCallback(async () => {
        if (!validate()) return;

        const patch = buildPatch();
        if (Object.keys(patch).length === 0) {
            onClose(); // nothing changed
            return;
        }

        const updated = await onUpdate(branch.id, patch);
        if (updated) onSaved(updated);
    }, [name, code, phone, email, isActive, branch, onUpdate, onSaved, onClose]);

    const isDirty =
        name.trim() !== branch.name ||
        code.trim().toUpperCase() !== branch.code ||
        (phone.trim() || null) !== branch.phone ||
        (email.trim() || null) !== branch.email ||
        isActive !== branch.is_active;

    return (
        <div className="flex flex-col h-full bg-white border-l border-slate-200">

            {/* Header */}
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div>
                    <h2 className="text-base font-bold text-ink">Edit Branch</h2>
                    <p className="text-xs text-ink-muted mt-0.5 font-mono">{branch.code}</p>
                </div>
                <button
                    onClick={onClose}
                    className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                >
                    <X className="w-4 h-4" />
                </button>
            </div>

            {/* Server error */}
            {saveError && (
                <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600 flex-shrink-0">
                    <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                    {saveError}
                </div>
            )}

            {/* Body */}
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">

                {/* Name */}
                <div>
                    <label className={labelCls}>
                        Branch Name <span className="text-red-400">*</span>
                    </label>
                    <div className="relative">
                        <Building2 className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                        <input
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            className={`${inputCls} pl-9 ${errors.name ? "border-red-300 bg-red-50/30" : ""}`}
                        />
                    </div>
                    {errors.name && (
                        <p className={fieldErrCls}><AlertCircle className="w-3 h-3" />{errors.name}</p>
                    )}
                </div>

                {/* Code */}
                <div>
                    <label className={labelCls}>
                        Branch Code <span className="text-red-400">*</span>
                    </label>
                    <div className="relative">
                        <Hash className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                        <input
                            value={code}
                            onChange={(e) => setCode(e.target.value.toUpperCase())}
                            className={`${inputCls} pl-9 font-mono ${errors.code ? "border-red-300 bg-red-50/30" : ""}`}
                        />
                    </div>
                    {errors.code && (
                        <p className={fieldErrCls}><AlertCircle className="w-3 h-3" />{errors.code}</p>
                    )}
                </div>

                {/* Phone + Email */}
                <div className="grid grid-cols-2 gap-3">
                    <div>
                        <label className={labelCls}>Phone</label>
                        <div className="relative">
                            <Phone className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                type="tel"
                                value={phone}
                                onChange={(e) => setPhone(e.target.value)}
                                placeholder="+233 XX XXX XXXX"
                                className={`${inputCls} pl-9 ${errors.phone ? "border-red-300 bg-red-50/30" : ""}`}
                            />
                        </div>
                        {errors.phone && (
                            <p className={fieldErrCls}><AlertCircle className="w-3 h-3" />{errors.phone}</p>
                        )}
                    </div>
                    <div>
                        <label className={labelCls}>Email</label>
                        <div className="relative">
                            <Mail className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                placeholder="branch@example.com"
                                className={`${inputCls} pl-9 ${errors.email ? "border-red-300 bg-red-50/30" : ""}`}
                            />
                        </div>
                        {errors.email && (
                            <p className={fieldErrCls}><AlertCircle className="w-3 h-3" />{errors.email}</p>
                        )}
                    </div>
                </div>

                {/* Active toggle */}
                <div className={`flex items-center justify-between py-3 px-4 rounded-xl border ${isActive ? "bg-green-50/50 border-green-200" : "bg-slate-50 border-slate-200"
                    }`}>
                    <div>
                        <p className="text-sm font-semibold text-ink">Branch Active</p>
                        <p className="text-xs text-slate-400 mt-0.5">
                            {isActive
                                ? "Branch is accepting transactions"
                                : "Branch is closed — no transactions allowed"}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => setIsActive((v) => !v)}
                        className="flex-shrink-0"
                    >
                        {isActive
                            ? <ToggleRight className="w-8 h-8 text-brand-600" />
                            : <ToggleLeft className="w-8 h-8 text-slate-300" />
                        }
                    </button>
                </div>

                {/* Dirty indicator */}
                {isDirty && (
                    <p className="text-xs text-amber-600 flex items-center gap-1 px-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-500 inline-block" />
                        You have unsaved changes
                    </p>
                )}
            </div>

            {/* Footer */}
            <div className="flex-shrink-0 border-t border-slate-200 px-4 py-4 flex items-center justify-end gap-3">
                <button
                    onClick={onClose}
                    type="button"
                    disabled={saving}
                    className="px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                    Cancel
                </button>
                <button
                    onClick={handleSave}
                    disabled={saving || !isDirty}
                    type="button"
                    className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
                >
                    {saving ? (
                        <>
                            <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                            </svg>
                            Saving…
                        </>
                    ) : (
                        <>
                            <Save className="w-4 h-4" />
                            Save Changes
                        </>
                    )}
                </button>
            </div>
        </div>
    );
}