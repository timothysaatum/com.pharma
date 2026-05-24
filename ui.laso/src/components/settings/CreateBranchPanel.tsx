/**
 * ─────────────────────────────────────────────────────────────
 * Slide-over panel for creating a new branch.
 *
 * Fields mirror BranchCreate / BranchBase schema exactly:
 *   name*, code (auto-suggested), phone, email,
 *   address (structured BranchAddress), manager_id,
 *   is_active toggle
 *
 * Operating hours deliberately omitted from creation —
 * they can be set via edit after the branch is live.
 * This keeps the creation flow fast and focused.
 *
 * Security: create button is only rendered for admin/super_admin
 * (enforced server-side too via require_role).
 */

import { useState, useCallback, useEffect } from "react";
import {
    X, Building2, Hash, Phone, Mail,
    MapPin, AlertCircle, Plus,
    ToggleLeft, ToggleRight,
} from "lucide-react";
import type { BranchCreate } from "@/types";

interface Props {
    onSubmit: (data: BranchCreate) => Promise<boolean>;
    onClose: () => void;
    submitting: boolean;
    submitError: string | null;
}

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 " +
    "transition-colors placeholder:text-slate-400";

const labelCls =
    "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

const fieldErrCls = "text-xs text-red-500 mt-1 flex items-center gap-1";

// Auto-generate a code suggestion from the branch name:
// "Accra Central" → "ACCRA-CENTRAL", capped at 20 chars
function suggestCode(name: string): string {
    return name
        .trim()
        .toUpperCase()
        .replace(/[^A-Z0-9\s]/g, "")
        .replace(/\s+/g, "-")
        .slice(0, 20);
}

export function CreateBranchPanel({ onSubmit, onClose, submitting, submitError }: Props) {
    // ── Form state ────────────────────────────────────────────
    const [name, setName] = useState("");
    const [code, setCode] = useState("");
    const [codeTouched, setCodeTouched] = useState(false);
    const [phone, setPhone] = useState("");
    const [email, setEmail] = useState("");
    const [isActive, setIsActive] = useState(true);

    // Address
    const [street, setStreet] = useState("");
    const [city, setCity] = useState("");
    const [state, setState] = useState("");
    const [zipCode, setZipCode] = useState("");
    const [country, setCountry] = useState("Ghana");

    const [errors, setErrors] = useState<Record<string, string>>({});

    // Auto-suggest code from name unless user has manually typed a code
    useEffect(() => {
        if (!codeTouched && name) {
            setCode(suggestCode(name));
        }
    }, [name, codeTouched]);

    // ── Validation — mirrors BranchBase schema ────────────────
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

    // ── Submit ────────────────────────────────────────────────
    const handleSubmit = useCallback(async () => {
        if (!validate()) return;

        const hasAddress = street || city || state || zipCode || country !== "Ghana";

        const payload: BranchCreate = {
            name: name.trim(),
            code: code.trim().toUpperCase(),
            phone: phone.trim() || undefined,
            email: email.trim() || undefined,
            is_active: isActive,
            address: hasAddress
                ? {
                    street: street.trim() || undefined,
                    city: city.trim() || undefined,
                    state: state.trim() || undefined,
                    zip_code: zipCode.trim() || undefined,
                    country: country.trim() || "Ghana",
                }
                : undefined,
        };

        const ok = await onSubmit(payload);
        if (ok) onClose();
    }, [name, code, phone, email, isActive, street, city, state, zipCode, country, onSubmit, onClose]);

    return (
        <div className="flex flex-col h-full bg-white border-l border-slate-200">

            {/* Header */}
            <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 flex-shrink-0">
                <div>
                    <h2 className="text-base font-bold text-ink">New Branch</h2>
                    <p className="text-xs text-ink-muted mt-0.5">
                        Add a new location to your organisation
                    </p>
                </div>
                <button
                    onClick={onClose}
                    className="p-1.5 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                >
                    <X className="w-4 h-4" />
                </button>
            </div>

            {/* Server error */}
            {submitError && (
                <div className="mx-4 mt-3 flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-xs text-red-600 flex-shrink-0">
                    <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                    {submitError}
                </div>
            )}

            {/* Scrollable body */}
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
                            placeholder="e.g. Accra Central"
                            autoFocus
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
                            onChange={(e) => {
                                setCodeTouched(true);
                                setCode(e.target.value.toUpperCase());
                            }}
                            placeholder="e.g. ACCRA-CENTRAL"
                            className={`${inputCls} pl-9 font-mono ${errors.code ? "border-red-300 bg-red-50/30" : ""}`}
                        />
                    </div>
                    {errors.code ? (
                        <p className={fieldErrCls}><AlertCircle className="w-3 h-3" />{errors.code}</p>
                    ) : (
                        <p className="text-[10px] text-slate-400 mt-1">
                            Unique identifier — auto-suggested from name, editable
                        </p>
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

                {/* Address */}
                <div>
                    <label className={labelCls}>
                        <MapPin className="inline w-3 h-3 mr-1 mb-0.5" />
                        Address
                    </label>
                    <div className="space-y-2">
                        <input
                            value={street}
                            onChange={(e) => setStreet(e.target.value)}
                            placeholder="Street address"
                            className={inputCls}
                        />
                        <div className="grid grid-cols-2 gap-2">
                            <input
                                value={city}
                                onChange={(e) => setCity(e.target.value)}
                                placeholder="City"
                                className={inputCls}
                            />
                            <input
                                value={state}
                                onChange={(e) => setState(e.target.value)}
                                placeholder="Region / State"
                                className={inputCls}
                            />
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <input
                                value={zipCode}
                                onChange={(e) => setZipCode(e.target.value)}
                                placeholder="Postal / ZIP code"
                                className={inputCls}
                            />
                            <input
                                value={country}
                                onChange={(e) => setCountry(e.target.value)}
                                placeholder="Country"
                                className={inputCls}
                            />
                        </div>
                    </div>
                </div>

                {/* Active toggle */}
                <div className="flex items-center justify-between py-3 px-4 rounded-xl bg-slate-50 border border-slate-200">
                    <div>
                        <p className="text-sm font-semibold text-ink">Active on creation</p>
                        <p className="text-xs text-slate-400 mt-0.5">
                            Inactive branches cannot process transactions
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

                {/* Hint about operating hours */}
                <p className="text-xs text-slate-400 px-1">
                    Operating hours can be configured after the branch is created.
                </p>
            </div>

            {/* Footer */}
            <div className="flex-shrink-0 border-t border-slate-200 px-4 py-4 flex items-center justify-end gap-3">
                <button
                    onClick={onClose}
                    type="button"
                    disabled={submitting}
                    className="px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                    Cancel
                </button>
                <button
                    onClick={handleSubmit}
                    disabled={submitting}
                    type="button"
                    className="px-5 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
                >
                    {submitting ? (
                        <>
                            <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                            </svg>
                            Creating…
                        </>
                    ) : (
                        <>
                            <Plus className="w-4 h-4" />
                            Create Branch
                        </>
                    )}
                </button>
            </div>
        </div>
    );
}