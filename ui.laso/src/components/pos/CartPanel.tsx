/**
 * CartPanel — right side of POS
 *
 * Layout strategy (bulletproof):
 *   - Outer div: flex-col h-full
 *   - Sticky header (flex-shrink-0)
 *   - ONE scrollable middle zone (flex-1 overflow-y-auto) containing:
 *       • cart item rows
 *       • divider
 *       • checkout form fields
 *   - Sticky footer: totals + Complete Sale button (flex-shrink-0)
 *
 * This avoids ALL nested-flex height issues by having only ONE scroll container.
 */

import { useEffect, useRef, useState } from "react";
import {
    Trash2, Plus, Minus, ShieldAlert, ChevronDown,
    User, FileText, AlertCircle, ShoppingCart, Package,
    Receipt, Banknote, Tag, Search, UserCheck, X, Loader2,
} from "lucide-react";
import type { AvailableContract } from "@/api/contracts";
import { PaymentMethod } from "@/types";
import { CartItem, CartTotals, CartValidationError } from "@/hooks/useCart";
import { apiClient } from "@/api/client";

const CONTRACT_TYPE_COLORS: Record<string, string> = {
    standard: "bg-slate-100 text-slate-600",
    insurance: "bg-blue-50  text-blue-700",
    corporate: "bg-purple-50 text-purple-700",
    staff: "bg-green-50 text-green-700",
    senior_citizen: "bg-amber-50 text-amber-700",
    wholesale: "bg-orange-50 text-orange-700",
    promotional: "bg-pink-50  text-pink-700",
};

const PAYMENT_METHODS: Array<{ value: PaymentMethod; label: string }> = [
    { value: "cash", label: "Cash" },
    { value: "card", label: "Card" },
    { value: "mobile_money", label: "Mobile Money" },
    { value: "insurance", label: "Insurance" },
    { value: "credit", label: "Credit" },
    { value: "split", label: "Split" },
];

// ─── Customer Search ─────────────────────────────────────────────────────────

interface CustomerMatch {
    id: string;
    full_name: string;
    phone: string | null;
    email: string | null;
    loyalty_tier: string | null;
    has_insurance: boolean;
    contract_name: string | null;
}

interface CustomerSearchWidgetProps {
    customerName: string;
    customerId: string | null;
    // onSetCustomerId: (id: string | null) => void;
    onSetCustomerName: (name: string) => void;
    onSetCustomerId: (id: string | null) => void;
    fieldError?: string;
}

function CustomerSearchWidget({
    customerName, customerId, onSetCustomerName, onSetCustomerId, fieldError,
}: CustomerSearchWidgetProps) {
    const [query, setQuery] = useState("");
    const [results, setResults] = useState<CustomerMatch[]>([]);
    const [searching, setSearching] = useState(false);
    const [open, setOpen] = useState(false);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const wrapperRef = useRef<HTMLDivElement>(null);

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, []);

    const search = (q: string) => {
        if (debounceRef.current) clearTimeout(debounceRef.current);
        if (q.length < 2) { setResults([]); setOpen(false); return; }
        debounceRef.current = setTimeout(async () => {
            setSearching(true);
            try {
                const { data } = await apiClient.get<{ matches: CustomerMatch[] }>(
                    "/customers/search",
                    { params: { q, limit: 10 } }
                );
                setResults(data.matches ?? []);
                setOpen(true);
            } catch {
                setResults([]);
            } finally {
                setSearching(false);
            }
        }, 300);
    };

    const selectCustomer = (c: CustomerMatch) => {
        onSetCustomerId(c.id);
        onSetCustomerName(c.full_name);
        setQuery("");
        setResults([]);
        setOpen(false);
    };

    const clearCustomer = () => {
        onSetCustomerId(null);
        onSetCustomerName("");
        setQuery("");
        setResults([]);
    };

    // If a registered customer is selected, show their card
    if (customerId) {
        return (
            <div className="flex items-center gap-2 p-2.5 rounded-lg bg-brand-50 border border-brand-100">
                <UserCheck className="w-4 h-4 text-brand-600 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-brand-700 truncate">{customerName}</p>
                    <p className="text-[10px] text-brand-500">Registered customer</p>
                </div>
                <button
                    type="button"
                    onClick={clearCustomer}
                    className="p-1 rounded text-brand-400 hover:text-brand-700 hover:bg-brand-100 transition-colors"
                    title="Remove customer"
                >
                    <X className="w-3.5 h-3.5" />
                </button>
            </div>
        );
    }

    return (
        <div ref={wrapperRef} className="space-y-2">
            {/* Search input */}
            <div className="relative">
                <Search className="absolute left-3 top-3 w-3.5 h-3.5 text-slate-400" />
                <input
                    value={query}
                    onChange={(e) => { setQuery(e.target.value); search(e.target.value); }}
                    placeholder="Search by name, phone, email…"
                    className={`${inputCls} pl-9 pr-8 ${fieldError ? "border-red-300 bg-red-50/30" : ""}`}
                />
                {searching && (
                    <Loader2 className="absolute right-3 top-3 w-3.5 h-3.5 text-slate-400 animate-spin" />
                )}
            </div>

            {/* Walk-in name (when no registered customer selected) */}
            {!query && (
                <div className="relative">
                    <User className="absolute left-3 top-3 w-3.5 h-3.5 text-slate-400" />
                    <input
                        value={customerName}
                        onChange={(e) => onSetCustomerName(e.target.value)}
                        placeholder="Or type walk-in customer name"
                        className={`${inputCls} pl-9 ${fieldError ? "border-red-300 bg-red-50/30" : ""}`}
                    />
                </div>
            )}

            {/* Dropdown results */}
            {open && results.length > 0 && (
                <div className="absolute z-50 mt-1 w-full bg-white border border-slate-200 rounded-xl shadow-lg overflow-hidden max-h-52 overflow-y-auto">
                    {results.map((c) => (
                        <button
                            key={c.id}
                            type="button"
                            onClick={() => selectCustomer(c)}
                            className="w-full flex items-start gap-2.5 px-3 py-2.5 text-left hover:bg-slate-50 border-b border-slate-50 last:border-0 transition-colors"
                        >
                            <UserCheck className="w-4 h-4 text-brand-500 flex-shrink-0 mt-0.5" />
                            <div className="min-w-0 flex-1">
                                <p className="text-sm font-semibold text-ink truncate">{c.full_name}</p>
                                <p className="text-xs text-slate-400 truncate">
                                    {[c.phone, c.email].filter(Boolean).join(" · ")}
                                    {c.loyalty_tier && (
                                        <span className="ml-1.5 capitalize font-medium text-amber-600">
                                            {c.loyalty_tier}
                                        </span>
                                    )}
                                    {c.has_insurance && (
                                        <span className="ml-1.5 text-blue-600 font-medium">Insurance</span>
                                    )}
                                </p>
                            </div>
                        </button>
                    ))}
                </div>
            )}

            {open && query.length >= 2 && results.length === 0 && !searching && (
                <p className="text-xs text-slate-400 px-1">No customers found — sale will be recorded as walk-in</p>
            )}

            {fieldError && (
                <p className="text-xs text-red-500 flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" />{fieldError}
                </p>
            )}
        </div>
    );
}

interface CartPanelProps {
    items: CartItem[];
    contract: AvailableContract | null;
    contracts: AvailableContract[];
    contractsLoading: boolean;
    customerName: string;
    customerId: string | null;
    paymentMethod: PaymentMethod;
    amountPaid: number;
    prescriptionId: string | null;
    insuranceClaimNumber: string;
    insurancePreAuthNumber: string;
    insuranceVerified: boolean;
    notes: string;
    totals: CartTotals;
    validationErrors: CartValidationError[];
    isSubmitting: boolean;

    onSetQuantity: (drugId: string, qty: number) => void;
    onRemoveItem: (drugId: string) => void;
    onSetPrescriptionVerified: (drugId: string, verified: boolean) => void;
    onSetContract: (contract: AvailableContract | null) => void;
    onSetCustomerId: (id: string | null) => void;
    onSetCustomerName: (name: string) => void;
    onSetPaymentMethod: (method: PaymentMethod) => void;
    onSetAmountPaid: (amount: number) => void;
    onSetPrescriptionId: (id: string | null) => void;
    onSetInsuranceClaimNumber: (n: string) => void;
    onSetInsurancePreAuthNumber: (n: string) => void;
    onSetInsuranceVerified: (v: boolean) => void;
    onSetNotes: (n: string) => void;
    onCheckout: () => void;
    onClearCart: () => void;
}

const inputCls =
    "w-full h-10 px-3 rounded-lg border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors";


function SectionLabel({ icon: Icon, children }: { icon: React.ElementType; children: React.ReactNode }) {
    return (
        <div className="flex items-center gap-2 mb-3">
            <Icon className="w-3.5 h-3.5 text-ink-muted" />
            <span className="text-[11px] font-bold text-ink-muted uppercase tracking-widest">{children}</span>
        </div>
    );
}

export function CartPanel({
    items, contract, contracts, contractsLoading,
    customerName, customerId, paymentMethod, amountPaid,
    prescriptionId, insuranceClaimNumber, insurancePreAuthNumber,
    insuranceVerified, notes, totals, validationErrors,
    isSubmitting, onSetQuantity, onRemoveItem, onSetPrescriptionVerified,
    onSetContract, onSetCustomerId, onSetCustomerName, onSetPaymentMethod, onSetAmountPaid,
    onSetPrescriptionId, onSetInsuranceClaimNumber, onSetInsurancePreAuthNumber,
    onSetInsuranceVerified, onSetNotes, onCheckout, onClearCart,
}: CartPanelProps) {
    const autoSelectedRef = useRef(false);
    useEffect(() => {
        if (!autoSelectedRef.current && contracts.length > 0 && !contract) {
            const def = contracts.find((c) => c.is_default) ?? contracts[0];
            onSetContract(def);
            autoSelectedRef.current = true;
        }
    }, [contracts, contract, onSetContract]);

    const hasRxItems = items.some((i) => i.requiresPrescription);
    const isInsurance = paymentMethod === "insurance" || contract?.type === "insurance";
    const fieldError = (field: string) =>
        validationErrors.find((e) => e.field === field)?.message;
    const hasErrors = validationErrors.length > 0 && items.length > 0;
    const isEmpty = items.length === 0;

    return (
        <div className="flex flex-col h-full bg-white">

            {/* ═══ STICKY HEADER ═══ */}
            <div className="flex-shrink-0 flex items-center justify-between px-5 py-3.5 border-b border-slate-100 bg-white">
                <div className="flex items-center gap-2.5">
                    <div className={`p-1.5 rounded-lg ${isEmpty ? "bg-slate-100" : "bg-brand-50"}`}>
                        <ShoppingCart className={`w-4 h-4 ${isEmpty ? "text-slate-400" : "text-brand-600"}`} />
                    </div>
                    <span className="text-sm font-bold text-ink">Cart</span>
                    {!isEmpty && (
                        <span className="inline-flex items-center justify-center min-w-[22px] h-5 px-1.5 text-[11px] font-bold bg-brand-600 text-white rounded-full">
                            {items.length}
                        </span>
                    )}
                </div>
                {!isEmpty && (
                    <button
                        onClick={onClearCart}
                        type="button"
                        className="text-xs text-slate-400 hover:text-red-500 font-medium transition-colors flex items-center gap-1"
                    >
                        <Trash2 className="w-3 h-3" />
                        Clear
                    </button>
                )}
            </div>

            {/* ═══ SINGLE SCROLL ZONE ═══ */}
            <div className="flex-1 overflow-y-auto min-h-0">

                {/* ── Empty state ── */}
                {isEmpty ? (
                    <div className="flex flex-col items-center justify-center h-full gap-4 px-8 py-16 text-center">
                        <div className="w-20 h-20 rounded-3xl bg-slate-50 border-2 border-dashed border-slate-200 flex items-center justify-center">
                            <Package className="w-9 h-9 text-slate-300" />
                        </div>
                        <div>
                            <p className="text-sm font-semibold text-slate-500">Cart is empty</p>
                            <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                                Search for a drug on the left<br />and tap <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-[10px] font-mono text-slate-500">+</kbd> to add it
                            </p>
                        </div>
                    </div>
                ) : (
                    <div className="px-4 pt-4 pb-2 space-y-2">

                        {/* ── Cart items ── */}
                        {items.map((item, idx) => (
                            <div
                                key={item.drug.id}
                                className="group relative rounded-xl border border-slate-200 bg-white hover:border-slate-300 hover:shadow-sm transition-all duration-150"
                            >
                                {/* Index pip */}
                                <div className="absolute -left-0 top-3.5 w-5 h-5 -ml-2.5 rounded-full bg-slate-200 flex items-center justify-center text-[9px] font-bold text-slate-500 z-10">
                                    {idx + 1}
                                </div>

                                <div className="px-4 pt-3 pb-2.5 pl-5">
                                    {/* Row 1: name + price */}
                                    <div className="flex items-start justify-between gap-2 mb-2.5">
                                        <div className="min-w-0">
                                            <p className="text-sm font-bold text-ink leading-tight truncate pr-2">
                                                {item.drug.name}
                                            </p>
                                            <p className="text-xs text-slate-400 mt-0.5">
                                                {item.drug.strength && <span>{item.drug.strength} · </span>}
                                                <span className="font-semibold text-slate-500">₵{item.drug.unit_price.toFixed(2)}</span>
                                                {" ea"}
                                            </p>
                                        </div>
                                        <div className="text-right flex-shrink-0">
                                            <p className="text-sm font-bold text-ink">
                                                ₵{(item.drug.unit_price * item.quantity).toFixed(2)}
                                            </p>
                                        </div>
                                    </div>

                                    {/* Row 2: qty + remove */}
                                    <div className="flex items-center justify-between">
                                        <div className="flex items-center bg-slate-50 border border-slate-200 rounded-lg overflow-hidden">
                                            <button
                                                onClick={() => onSetQuantity(item.drug.id, item.quantity - 1)}
                                                type="button"
                                                className="w-8 h-8 flex items-center justify-center text-slate-500 hover:text-ink hover:bg-slate-100 transition-colors"
                                            >
                                                <Minus className="w-3 h-3" />
                                            </button>
                                            <input
                                                type="number"
                                                min={1}
                                                max={1000}
                                                value={item.quantity}
                                                onChange={(e) =>
                                                    onSetQuantity(item.drug.id, parseInt(e.target.value) || 1)
                                                }
                                                className="w-12 h-8 text-center text-sm font-bold bg-white border-x border-slate-200 focus:outline-none focus:bg-white"
                                            />
                                            <button
                                                onClick={() => onSetQuantity(item.drug.id, item.quantity + 1)}
                                                type="button"
                                                className="w-8 h-8 flex items-center justify-center text-slate-500 hover:text-ink hover:bg-slate-100 transition-colors"
                                            >
                                                <Plus className="w-3 h-3" />
                                            </button>
                                        </div>

                                        <button
                                            onClick={() => onRemoveItem(item.drug.id)}
                                            type="button"
                                            className="text-xs text-slate-400 hover:text-red-500 transition-colors flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-red-50"
                                        >
                                            <Trash2 className="w-3 h-3" />
                                            Remove
                                        </button>
                                    </div>

                                    {/* Rx badge */}
                                    {item.requiresPrescription && (
                                        <div className={`flex items-center gap-2 mt-2.5 px-3 py-2 rounded-lg text-xs border ${item.prescriptionVerified
                                            ? "bg-green-50 border-green-100 text-green-700"
                                            : "bg-violet-50 border-violet-100 text-violet-700"
                                            }`}>
                                            <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0" />
                                            <span className="flex-1 font-medium">Rx required</span>
                                            <label className="flex items-center gap-1.5 cursor-pointer font-semibold">
                                                <input
                                                    type="checkbox"
                                                    checked={item.prescriptionVerified}
                                                    onChange={(e) => onSetPrescriptionVerified(item.drug.id, e.target.checked)}
                                                    className="w-3.5 h-3.5 rounded accent-green-600"
                                                />
                                                Verified
                                            </label>
                                        </div>
                                    )}
                                </div>
                            </div>
                        ))}

                        {/* ── Divider into checkout form ── */}
                        <div className="flex items-center gap-3 py-3">
                            <div className="flex-1 h-px bg-slate-100" />
                            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Checkout</span>
                            <div className="flex-1 h-px bg-slate-100" />
                        </div>

                        {/* ── Checkout form ── */}
                        <div className="space-y-4 pb-4">

                            {/* Price Contract */}
                            <div>
                                <SectionLabel icon={Tag}>Price Contract</SectionLabel>
                                <div className="relative">
                                    <select
                                        value={contract?.id ?? ""}
                                        onChange={(e) => {
                                            const c = contracts.find((x) => x.id === e.target.value) ?? null;
                                            onSetContract(c);
                                        }}
                                        disabled={contractsLoading}
                                        className={`${inputCls} appearance-none pr-8 ${fieldError("contract") ? "border-red-300 bg-red-50/30" : ""}`}
                                    >
                                        {contractsLoading ? (
                                            <option>Loading…</option>
                                        ) : (
                                            <>
                                                <option value="">— Select contract —</option>
                                                {contracts.map((c) => (
                                                    <option key={c.id} value={c.id}>{c.display}</option>
                                                ))}
                                            </>
                                        )}
                                    </select>
                                    <ChevronDown className="absolute right-3 top-3 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                                </div>
                                {contract && (
                                    <div className="flex items-center gap-2 mt-2 flex-wrap">
                                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${CONTRACT_TYPE_COLORS[contract.type] ?? "bg-slate-100 text-slate-600"}`}>
                                            {contract.type.replace("_", " ").toUpperCase()}
                                        </span>
                                        {contract.discount_percentage > 0 && (
                                            <span className="text-xs text-green-600 font-semibold bg-green-50 px-2 py-0.5 rounded-full">
                                                {contract.discount_percentage}% off
                                            </span>
                                        )}
                                        {contract.warning && (
                                            <span className="text-xs text-amber-600 flex items-center gap-1">
                                                <AlertCircle className="w-3 h-3" />{contract.warning}
                                            </span>
                                        )}
                                    </div>
                                )}
                                {fieldError("contract") && (
                                    <p className="text-xs text-red-500 mt-1.5 flex items-center gap-1">
                                        <AlertCircle className="w-3 h-3" />{fieldError("contract")}
                                    </p>
                                )}
                            </div>

                            {/* Customer */}
                            <div className="relative">
                                <SectionLabel icon={User}>Customer</SectionLabel>
                                <CustomerSearchWidget
                                    customerName={customerName}
                                    customerId={customerId}
                                    onSetCustomerName={onSetCustomerName}
                                    onSetCustomerId={onSetCustomerId}
                                    fieldError={fieldError("customer")}
                                />
                            </div>

                            {/* Prescription ID */}
                            {hasRxItems && (
                                <div>
                                    <SectionLabel icon={FileText}>Prescription</SectionLabel>
                                    {!customerId && (
                                        <div className="flex items-start gap-2 p-2.5 rounded-lg bg-amber-50 border border-amber-100 mb-2">
                                            <AlertCircle className="w-3.5 h-3.5 text-amber-600 flex-shrink-0 mt-0.5" />
                                            <p className="text-xs text-amber-700">
                                                A <strong>registered customer</strong> must be selected above before linking a prescription.
                                            </p>
                                        </div>
                                    )}
                                    <div className="relative">
                                        <FileText className="absolute left-3 top-3 w-3.5 h-3.5 text-slate-400" />
                                        <input
                                            value={prescriptionId ?? ""}
                                            onChange={(e) => onSetPrescriptionId(e.target.value || null)}
                                            placeholder="Prescription UUID from patient record"
                                            disabled={!customerId}
                                            className={`${inputCls} pl-9 font-mono text-xs ${!customerId ? "opacity-50 cursor-not-allowed bg-slate-50" : ""} ${fieldError("prescription_id") ? "border-red-300 bg-red-50/30" : ""}`}
                                        />
                                    </div>
                                    <p className="text-[10px] text-slate-400 mt-1.5">
                                        The prescription must be active, have refills remaining, and belong to the selected customer.
                                    </p>
                                    {fieldError("prescription_id") && (
                                        <p className="text-xs text-red-500 mt-1.5 flex items-center gap-1">
                                            <AlertCircle className="w-3 h-3" />{fieldError("prescription_id")}
                                        </p>
                                    )}
                                </div>
                            )}

                            {/* Insurance */}
                            {isInsurance && (
                                <div className="space-y-2.5 p-3.5 rounded-xl bg-blue-50 border border-blue-100">
                                    <p className="text-[11px] font-bold text-blue-700 uppercase tracking-widest">Insurance Details</p>
                                    <input
                                        value={insuranceClaimNumber}
                                        onChange={(e) => onSetInsuranceClaimNumber(e.target.value)}
                                        placeholder="Claim number *"
                                        className={`${inputCls} border-blue-200 bg-white ${fieldError("insurance_claim") ? "border-red-300" : ""}`}
                                    />
                                    <input
                                        value={insurancePreAuthNumber}
                                        onChange={(e) => onSetInsurancePreAuthNumber(e.target.value)}
                                        placeholder="Pre-auth number (optional)"
                                        className={`${inputCls} border-blue-200 bg-white`}
                                    />
                                    <label className="flex items-center gap-2 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={insuranceVerified}
                                            onChange={(e) => onSetInsuranceVerified(e.target.checked)}
                                            className="w-4 h-4 rounded accent-blue-600"
                                        />
                                        <span className="text-sm font-semibold text-blue-700">Card verified ✓</span>
                                    </label>
                                    {fieldError("insurance") && <p className="text-xs text-red-500">{fieldError("insurance")}</p>}
                                </div>
                            )}

                            {/* Payment method */}
                            <div>
                                <SectionLabel icon={Banknote}>Payment Method</SectionLabel>
                                <div className="grid grid-cols-3 gap-1.5">
                                    {PAYMENT_METHODS.map((m) => (
                                        <button
                                            key={m.value}
                                            onClick={() => onSetPaymentMethod(m.value)}
                                            type="button"
                                            className={`py-2 text-xs font-semibold rounded-lg border transition-all ${paymentMethod === m.value
                                                ? "bg-brand-600 border-brand-600 text-white shadow-sm"
                                                : "bg-white border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50"
                                                }`}
                                        >
                                            {m.label}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Amount tendered */}
                            {(paymentMethod === "cash" || paymentMethod === "split") && (
                                <div>
                                    <SectionLabel icon={Banknote}>Amount Tendered (GHS)</SectionLabel>
                                    <div className="relative">
                                        <span className="absolute left-3 top-2.5 text-sm font-semibold text-slate-400">₵</span>
                                        <input
                                            type="number"
                                            min={0}
                                            step="0.01"
                                            value={amountPaid || ""}
                                            onChange={(e) => onSetAmountPaid(parseFloat(e.target.value) || 0)}
                                            placeholder={totals.total.toFixed(2)}
                                            className={`${inputCls} pl-7 ${fieldError("amount_paid") ? "border-red-300 bg-red-50/30" : ""}`}
                                        />
                                    </div>
                                    {fieldError("amount_paid") && (
                                        <p className="text-xs text-red-500 mt-1.5 flex items-center gap-1">
                                            <AlertCircle className="w-3 h-3" />{fieldError("amount_paid")}
                                        </p>
                                    )}
                                </div>
                            )}

                            {/* Notes */}
                            <div>
                                <SectionLabel icon={Receipt}>Notes (optional)</SectionLabel>
                                <input
                                    value={notes}
                                    onChange={(e) => onSetNotes(e.target.value)}
                                    placeholder="Any notes about this sale…"
                                    className={inputCls}
                                />
                            </div>
                        </div>
                    </div>
                )}
            </div>

            {/* ═══ STICKY FOOTER ═══ */}
            {!isEmpty && (
                <div className="flex-shrink-0 border-t border-slate-200 bg-white px-5 pt-3.5 pb-5">

                    {/* Totals */}
                    <div className="space-y-1.5 mb-3">
                        <div className="flex justify-between text-xs text-slate-500">
                            <span>Subtotal ({totals.itemCount} {totals.itemCount === 1 ? "item" : "items"})</span>
                            <span className="font-medium">₵{totals.subtotal.toFixed(2)}</span>
                        </div>
                        {totals.discountAmount > 0 && (
                            <div className="flex justify-between text-xs text-green-600 font-medium">
                                <span>Discount ({contract?.discount_percentage}%)</span>
                                <span>−₵{totals.discountAmount.toFixed(2)}</span>
                            </div>
                        )}
                        {totals.taxAmount > 0 && (
                            <div className="flex justify-between text-xs text-slate-500">
                                <span>Tax</span>
                                <span className="font-medium">₵{totals.taxAmount.toFixed(2)}</span>
                            </div>
                        )}
                        <div className="flex justify-between items-baseline pt-2 border-t border-slate-100">
                            <span className="text-sm font-bold text-ink">Total</span>
                            <span className="text-xl font-bold text-ink">₵{totals.total.toFixed(2)}</span>
                        </div>
                        {amountPaid > 0 && totals.change > 0 && (
                            <div className="flex justify-between text-sm font-bold text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-xl px-3 py-2 mt-1">
                                <span>Change due</span>
                                <span>₵{totals.change.toFixed(2)}</span>
                            </div>
                        )}
                    </div>

                    {/* Validation errors — compact */}
                    {hasErrors && (
                        <div className="rounded-xl bg-red-50 border border-red-100 px-3 py-2.5 mb-3 space-y-1">
                            {validationErrors.map((e) => (
                                <p key={e.field} className="text-xs text-red-600 flex items-start gap-1.5">
                                    <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                    {e.message}
                                </p>
                            ))}
                        </div>
                    )}

                    {/* CTA */}
                    <button
                        onClick={onCheckout}
                        disabled={isSubmitting}
                        type="button"
                        className="w-full py-3.5 text-sm font-bold text-white rounded-xl transition-all
                            bg-brand-600 hover:bg-brand-700 active:scale-[0.99]
                            disabled:opacity-50 disabled:cursor-not-allowed
                            flex items-center justify-center gap-2 shadow-sm"
                    >
                        {isSubmitting ? (
                            <>
                                <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                </svg>
                                Processing…
                            </>
                        ) : (
                            `Complete Sale · ₵${totals.total.toFixed(2)}`
                        )}
                    </button>
                </div>
            )}
        </div>
    );
}