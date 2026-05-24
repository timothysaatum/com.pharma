/**
 * CreateSupplierModal.tsx
 * ─────────────────────────────────────────────────────────────
 * Modal for creating a new supplier.
 *
 * Fields mirror SupplierCreate on the backend:
 *   name*, contact_person, email, phone, address, payment_terms,
 *   credit_limit, notes, is_active
 *
 * Permission: manage_suppliers (enforced server-side too).
 * On success: calls onCreated(supplier) so the parent can append
 * the new supplier to its local list without a full refetch.
 */

import { useState, useCallback } from "react";
import {
    X, Building2, User2, Mail, Phone,
    FileText, AlertCircle, Plus,
} from "lucide-react";
import type { Supplier, SupplierCreate } from "@/types";
import { useAuthStore } from "@/stores/authStore";

interface Props {
    onCreated: (supplier: Supplier) => void;
    onClose: () => void;
    submitting: boolean;
    submitError: string | null;
    onSubmit: (data: SupplierCreate) => Promise<Supplier | null>;
}

const inputCls =
    "w-full h-10 px-3 rounded-xl border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors " +
    "placeholder:text-slate-400";

const labelCls =
    "block text-xs font-semibold text-ink-muted uppercase tracking-widest mb-1.5";

const errorCls = "text-xs text-red-500 mt-1 flex items-center gap-1";

export function CreateSupplierModal({
    onCreated,
    onClose,
    submitting,
    submitError,
    onSubmit,
}: Props) {
    // ── Form state ────────────────────────────────────────────
    const [name, setName] = useState("");
    const [contactPerson, setContactPerson] = useState("");
    const [email, setEmail] = useState("");
    const [phone, setPhone] = useState("");
    // OrgAddress fields
    const [street, setStreet] = useState("");
    const [city, setCity] = useState("");
    const [state, setState] = useState("");
    const [country, setCountry] = useState("");
    const [paymentTerms, setPaymentTerms] = useState("");
    const [creditLimit, setCreditLimit] = useState<string>("");
    const { user } = useAuthStore();
    const [errors, setErrors] = useState<Record<string, string>>({});

    // ── Validation ────────────────────────────────────────────
    const validate = (): boolean => {
        const errs: Record<string, string> = {};

        if (!name.trim()) {
            errs.name = "Supplier name is required";
        } else if (name.trim().length < 2) {
            errs.name = "Name must be at least 2 characters";
        } else if (name.trim().length > 200) {
            errs.name = "Name must be under 200 characters";
        }

        if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            errs.email = "Enter a valid email address";
        }

        if (phone && !/^\+?[\d\s\-()]{7,20}$/.test(phone)) {
            errs.phone = "Enter a valid phone number";
        }

        if (creditLimit !== "" && (isNaN(Number(creditLimit)) || Number(creditLimit) < 0)) {
            errs.creditLimit = "Credit limit must be a positive number";
        }

        setErrors(errs);
        return Object.keys(errs).length === 0;
    };

    // ── Submit ────────────────────────────────────────────────
    const handleSubmit = useCallback(async () => {
        if (!validate()) return;

        // Build OrgAddress only if at least one field is filled
        const hasAddress = street.trim() || city.trim() || state.trim() || country.trim();
        const payload: SupplierCreate = {
            organization_id: user?.organization_id ?? "",
            name: name.trim(),
            contact_person: contactPerson.trim() || undefined,
            email: email.trim() || undefined,
            phone: phone.trim() || undefined,
            address: hasAddress
                ? {
                    street: street.trim() || undefined,
                    city: city.trim() || undefined,
                    state: state.trim() || undefined,
                    country: country.trim() || "GH",
                }
                : undefined,
            payment_terms: paymentTerms.trim() || undefined,
            credit_limit: creditLimit !== "" ? Number(creditLimit) : undefined,
        };

        const created = await onSubmit(payload);
        if (created) {
            onCreated(created);
            onClose();
        }
    }, [name, contactPerson, email, phone, street, city, state, country, paymentTerms, creditLimit, onSubmit, onCreated, onClose]);

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
            <div className="w-full max-w-lg bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]">

                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 flex-shrink-0">
                    <div>
                        <h2 className="text-lg font-bold text-ink">New Supplier</h2>
                        <p className="text-xs text-ink-muted mt-0.5">
                            Add a supplier to use in purchase orders
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 rounded-xl text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">

                    {/* Server error */}
                    {submitError && (
                        <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600">
                            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                            {submitError}
                        </div>
                    )}

                    {/* Name — required */}
                    <div>
                        <label className={labelCls}>
                            Supplier Name <span className="text-red-400">*</span>
                        </label>
                        <div className="relative">
                            <Building2 className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="e.g. Pharmanova Ghana Ltd"
                                className={`${inputCls} pl-9 ${errors.name ? "border-red-300 bg-red-50/30" : ""}`}
                                autoFocus
                            />
                        </div>
                        {errors.name && (
                            <p className={errorCls}>
                                <AlertCircle className="w-3 h-3" />{errors.name}
                            </p>
                        )}
                    </div>

                    {/* Contact person + Email */}
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={labelCls}>Contact Person</label>
                            <div className="relative">
                                <User2 className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                                <input
                                    value={contactPerson}
                                    onChange={(e) => setContactPerson(e.target.value)}
                                    placeholder="Full name"
                                    className={`${inputCls} pl-9`}
                                />
                            </div>
                        </div>
                        <div>
                            <label className={labelCls}>Email</label>
                            <div className="relative">
                                <Mail className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                                <input
                                    type="email"
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    placeholder="supplier@example.com"
                                    className={`${inputCls} pl-9 ${errors.email ? "border-red-300 bg-red-50/30" : ""}`}
                                />
                            </div>
                            {errors.email && (
                                <p className={errorCls}>
                                    <AlertCircle className="w-3 h-3" />{errors.email}
                                </p>
                            )}
                        </div>
                    </div>

                    {/* Phone + Credit limit */}
                    <div className="grid grid-cols-2 gap-4">
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
                                <p className={errorCls}>
                                    <AlertCircle className="w-3 h-3" />{errors.phone}
                                </p>
                            )}
                        </div>
                        <div>
                            <label className={labelCls}>Credit Limit (₵)</label>
                            <div className="relative">
                                <span className="absolute left-3 top-2.5 text-sm text-slate-400 font-semibold pointer-events-none">₵</span>
                                <input
                                    type="number"
                                    min={0}
                                    step={0.01}
                                    value={creditLimit}
                                    onChange={(e) => setCreditLimit(e.target.value)}
                                    placeholder="0.00"
                                    className={`${inputCls} pl-7 ${errors.creditLimit ? "border-red-300 bg-red-50/30" : ""}`}
                                />
                            </div>
                            {errors.creditLimit && (
                                <p className={errorCls}>
                                    <AlertCircle className="w-3 h-3" />{errors.creditLimit}
                                </p>
                            )}
                        </div>
                    </div>

                    {/* Address — structured OrgAddress */}
                    <div>
                        <label className={labelCls}>Address</label>
                        <div className="grid grid-cols-2 gap-3">
                            <input
                                value={street}
                                onChange={(e) => setStreet(e.target.value)}
                                placeholder="Street"
                                className={inputCls}
                            />
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
                            <input
                                value={country}
                                onChange={(e) => setCountry(e.target.value)}
                                placeholder="Country (e.g. GH)"
                                className={inputCls}
                            />
                        </div>
                    </div>

                    {/* Payment terms */}
                    <div>
                        <label className={labelCls}>Payment Terms</label>
                        <div className="relative">
                            <FileText className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                            <input
                                value={paymentTerms}
                                onChange={(e) => setPaymentTerms(e.target.value)}
                                placeholder="e.g. Net 30, COD, 50% upfront"
                                className={`${inputCls} pl-9`}
                            />
                        </div>
                    </div>

                </div>

                {/* Footer */}
                <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-slate-100 flex-shrink-0">
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
                                Saving…
                            </>
                        ) : (
                            <>
                                <Plus className="w-4 h-4" />
                                Add Supplier
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
}