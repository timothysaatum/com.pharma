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
import { pickDefaultContract } from "@/lib/pickDefaultContract";
import {
    getLineStockState,
    stockLabelClass,
    stockLabelText,
} from "@/lib/cartStockState";
import { PaymentMethod } from "@/types";
import { CartItem, CartTotals, CartValidationError, SplitPayment } from "@/hooks/useCart";
import { apiClient, isBackendKnownUnreachable } from "@/api/client";
import { localRead } from "@/lib/localRead";
import { appEvents } from "@/lib/events";
import { useAuthStore } from "@/stores/authStore";
import { PrescriptionSelector } from "@/components/pos/PrescriptionSelector";

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

function mergeCustomerMatches(
    primary: CustomerMatch[],
    secondary: CustomerMatch[],
    limit: number,
): CustomerMatch[] {
    const byId = new Map<string, CustomerMatch>();
    for (const match of [...primary, ...secondary]) {
        if (!byId.has(match.id)) byId.set(match.id, match);
    }
    return [...byId.values()].slice(0, limit);
}

interface CustomerSearchWidgetProps {
    customerName: string;
    customerId: string | null;
    // onSetCustomerId: (id: string | null) => void;
    onSetCustomerName: (name: string) => void;
    onSetCustomerId: (id: string | null) => void;
    requireRegistered?: boolean;
    fieldError?: string;
}

function CustomerSearchWidget({
    customerName, customerId, onSetCustomerName, onSetCustomerId, requireRegistered = false, fieldError,
}: CustomerSearchWidgetProps) {
    const organizationId = useAuthStore(
        (state) => state.user?.organization_id
    );
    const [query, setQuery] = useState("");
    const [results, setResults] = useState<CustomerMatch[]>([]);
    const [searching, setSearching] = useState(false);
    const [open, setOpen] = useState(false);
    const [isRegistered, setIsRegistered] = useState(requireRegistered || !!customerId);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const abortRef = useRef<AbortController | null>(null);
    const wrapperRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (requireRegistered) setIsRegistered(true);
    }, [requireRegistered]);

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

    // Cancel any in-flight search on unmount
    useEffect(() => {
        return () => abortRef.current?.abort();
    }, []);

    // A loyalty change published by the server (or by another device's sale) can
    // change the tier shown beside a matched customer. Re-run the active query so
    // the typeahead does not keep displaying a stale tier. Debounced, and it only
    // re-queries when there is something to re-query for, so a closed typeahead
    // costs nothing.
    useEffect(() => {
        if (!open || query.length < 2) return;
        let timer: ReturnType<typeof setTimeout> | null = null;
        const unsubscribe = appEvents.on("customers:changed", () => {
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => {
                search(query);
                // eslint-disable-next-line react-hooks/exhaustive-deps
            }, 400);
        });
        return () => {
            unsubscribe();
            if (timer) clearTimeout(timer);
        };
    }, [open, query]);

    const search = (q: string) => {
        if (debounceRef.current) clearTimeout(debounceRef.current);
        if (q.length < 2) { setResults([]); setOpen(false); return; }
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;
        debounceRef.current = setTimeout(async () => {
            setSearching(true);
            let localMatches: CustomerMatch[] = [];
            try {
                localMatches = await localRead
                    .searchCustomerMatches(q, 10, organizationId)
                    .catch(() => [] as CustomerMatch[]);

                if (!controller.signal.aborted) {
                    setResults(localMatches);
                    setOpen(localMatches.length > 0);
                }

                // When offline or the backend is known unreachable, skip the API
                // call entirely to avoid noisy connection-refused console errors.
                // The sync engine periodically retries connectivity and calls
                // markBackendOnline() on success, so this flag self-heals.
                if (!navigator.onLine || isBackendKnownUnreachable()) {
                    if (!controller.signal.aborted) {
                        setOpen(true);
                    }
                    return;
                }

                const { data } = await apiClient.get<{ matches: CustomerMatch[] }>(
                    "/customers/search",
                    { params: { q, limit: 10 }, signal: controller.signal, timeout: 3000 }
                );
                if (!controller.signal.aborted) {
                    const serverMatches = data.matches ?? [];
                    setResults(mergeCustomerMatches(serverMatches, localMatches, 10));
                    setOpen(true);
                }
            } catch (err: unknown) {
                if (controller.signal.aborted) return;
                // API call failed (offline / unreachable) — local results
                // are already displayed from the search above.
                if (!controller.signal.aborted) {
                    setResults(localMatches);
                    setOpen(localMatches.length > 0);
                }
            } finally {
                if (!controller.signal.aborted) setSearching(false);
            }
        }, 300);
    };

    const selectCustomer = (c: CustomerMatch) => {
        onSetCustomerName(c.full_name);
        onSetCustomerId(c.id);
        setQuery("");
        setResults([]);
        setOpen(false);
    };

    const clearCustomer = () => {
        onSetCustomerId(null);
        onSetCustomerName("");
        setQuery("");
        setResults([]);
        if (!requireRegistered) setIsRegistered(false);
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
            {!isRegistered ? (
                <>
                    <div className="relative">
                        <User className="absolute left-3 top-3 w-3.5 h-3.5 text-slate-400" />
                        <input
                            value={customerName}
                            onChange={(e) => onSetCustomerName(e.target.value)}
                            placeholder="Enter walk-in customer name"
                            className={`${inputCls} pl-9 ${fieldError ? "border-red-300 bg-red-50/30" : ""}`}
                        />
                    </div>
                    <button
                        type="button"
                        onClick={() => setIsRegistered(true)}
                        className="text-xs font-semibold text-brand-600 hover:text-brand-700 flex items-center gap-1 px-1 transition-colors"
                    >
                        <Search className="w-3 h-3" />
                        Select Registered Customer
                    </button>
                </>
            ) : (
                <>
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
                    {!requireRegistered && (
                        <button
                            type="button"
                            onClick={() => {
                                setIsRegistered(false);
                                setQuery("");
                                setResults([]);
                            }}
                            className="text-xs font-semibold text-slate-500 hover:text-slate-700 flex items-center gap-1 px-1 transition-colors"
                        >
                            <User className="w-3 h-3" />
                            Back to Walk-in
                        </button>
                    )}
                </>
            )}

            {!query && requireRegistered && (
                <p className="text-xs text-blue-600 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
                    Search and select a registered customer for insurance checkout.
                </p>
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
                <p className="text-xs text-slate-400 px-1">
                    {requireRegistered ? "No registered customer found" : "No customers found — sale will be recorded as walk-in"}
                </p>
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
    /**
     * Why the contract list is empty when it is. Drives an explicit message
     * plus a Retry button, so a failed load is never mistaken for "pick a
     * contract". `kind: null` means there is nothing to report.
     */
    contractsIssue?: { kind: 'offline' | 'error' | 'empty' | null; message: string | null } | null;
    onRetryContracts?: () => void;
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
    checkoutError: string | null;
    isSubmitting: boolean;
    taxInclusive?: boolean;
    stockQuantities?: Record<string, number>;

    onSetQuantity: (drugId: string, qty: number) => void;
    onRemoveItem: (drugId: string) => void;
    onSetPrescriptionVerified: (drugId: string, verified: boolean) => void;
    onSetContract: (contract: AvailableContract | null) => void;
    onSetCustomerId: (id: string | null) => void;
    onSetCustomerName: (name: string) => void;
    onSetPaymentMethod: (method: PaymentMethod) => void;
    onSetAmountPaid: (amount: number) => void;
    onSetSplitPayment: (split: Partial<SplitPayment>) => void;
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

/** Ceiling for one cart line when stock is unknown. Matches useCart's cap. */
export const LINE_QUANTITY_CAP = 1000;

/**
 * Sanitise a typed quantity draft: digits only, no leading zeros.
 *
 * Empty is allowed and returned as-is, because the field must be clearable.
 * Anything else non-numeric collapses to "" so a stray letter can never become
 * NaN and then silently commit as 1.
 */
export function sanitiseDraft(raw: string): string {
    const digits = raw.replace(/[^0-9]/g, "");
    if (digits === "") return "";
    const stripped = digits.replace(/^0+/, "");
    return stripped === "" ? "0" : stripped;
}

/**
 * Turn a draft into the quantity to commit.
 *
 * empty / NaN / < 1  -> 1
 * known available     -> clamped to available (the "max" state follows)
 * available unknown   -> the value, up to the reducer's existing 1000 cap
 */
export function commitDraft(
    draft: string,
    available: number | undefined,
    cap = LINE_QUANTITY_CAP
): number {
    const n = Number(draft);
    if (draft === "" || Number.isNaN(n) || n < 1) return 1;
    if (available !== undefined && !Number.isNaN(available)) {
        return Math.min(n, available);
    }
    return Math.min(n, cap);
}

export function CartPanel({
    items, contract, contracts, contractsLoading,
    contractsIssue, onRetryContracts,
    customerName, customerId, paymentMethod, amountPaid,
    prescriptionId, insuranceClaimNumber, insurancePreAuthNumber,
    insuranceVerified, notes, totals, validationErrors, checkoutError,
    isSubmitting, taxInclusive = false, stockQuantities = {},
    onSetQuantity, onRemoveItem, onSetPrescriptionVerified,
    onSetContract, onSetCustomerId, onSetCustomerName, onSetPaymentMethod, onSetAmountPaid,
    onSetSplitPayment, onSetPrescriptionId, onSetInsuranceClaimNumber, onSetInsurancePreAuthNumber,
    onSetInsuranceVerified, onSetNotes, onCheckout, onClearCart,
}: CartPanelProps) {
    // ── Quantity draft (per cart line) ──────────────────────────────────
    //
    // The committed quantity lives in the cart; while the field has focus it
    // shows a local draft string instead. Without this the input cannot be
    // cleared (parseInt("") || 1 snapped it straight back to 1), and typing
    // "133" into "1" without a select-on-focus produced 1133.
    const [qtyDraft, setQtyDraft] = useState<Record<string, string>>({});
    const qtyFocused = useRef<string | null>(null);
    // Set when a key handler has already settled the line (Enter) or abandoned
    // it (Escape), so the blur that follows does not commit the draft again.
    // Escape used to blur and then let onBlur commit the very draft it had just
    // discarded, because setQtyDraft has not applied yet inside that closure.
    const skipNextCommitRef = useRef<string | null>(null);

    /**
     * Lines whose typed quantity hit the 1000 ceiling, so the cashier is told
     * rather than left wondering why 5000 became 1000.
     *
     * Only the ceiling earns a note: a clamp to available stock is already
     * explained by the "Max reached (N)" label beside the stepper.
     */
    const [cappedLines, setCappedLines] = useState<Record<string, boolean>>({});

    // FIX: Track whether the user has manually edited the amount tendered.
    // When true, we stop auto-syncing so their typed value is preserved.
    // Reset to false whenever the payment method changes or the cart is cleared,
    // so the field tracks the total again on the next fresh session.
    const amountManuallyEdited = useRef(false);

    /**
     * Always keep a contract selected.
     *
     * THE BUG THIS REPLACES
     * --------------------
     * The previous version latched with `autoSelectedRef`, set to true on the
     * first successful selection and never reset anywhere. `contract` was in the
     * deps, so the effect DID re-run after a cart reset — but the latch
     * short-circuited it, so nothing was re-selected. Observed 2026-10-04: the
     * list loaded and held "STANDARD PRICE (Standard)", the cashier completed
     * sales, `clearCart()` nulled `contract`, and the picker sat on
     * "— Select contract —" with "Select a price contract" and a disabled sale
     * button for the rest of the session. Earlier in the same session it had
     * auto-selected, which is what made it look intermittent.
     *
     * THE RULE, IN ONE PLACE
     * ----------------------
     * Select the default when there is nothing valid selected. "Valid" means the
     * selected id is still in the current list, which covers every trigger with
     * one condition instead of one condition per trigger:
     *
     *   - initial load          contracts go [] -> [x], nothing selected
     *   - contract list change  a new list arrives
     *   - cart reset            CLEAR_CART -> INITIAL_STATE -> contract null.
     *                           Covers "New Sale", the success modal's close,
     *                           Clear, and the branch-change clear, because they
     *                           all dispatch the same action.
     *   - branch change         both of the above
     *   - selection no longer offered  selected id is absent from the list
     *
     * It goes through `onSetContract`, the same dispatch the picker's onChange
     * uses, so the payment-method and amountPaid side effects in
     * `SET_CONTRACT` (useCart.ts:189-215) still happen. Firing a bespoke
     * "just set the id" action here would silently skip the insurance ->
     * cash reset and leave the cashier on a payment method the new contract
     * does not accept.
     *
     * A valid selection is never overridden, so a contract the cashier chose for
     * this cart survives re-renders and list refreshes; it only gives way when
     * the cart resets or the contract stops being offered.
     *
     * An empty list selects nothing. That is also what keeps a FAILED load
     * (POSPage sets kind:'error'/'offline'/'empty' only when the list came back
     * empty) from quietly auto-picking something: no contracts, no selection,
     * error band stays, and Retry re-runs this once the list arrives.
     */
    useEffect(() => {
        if (contracts.length === 0) return;
        // Compare by id, not by object identity: a reload hands back new objects
        // for the same contracts, and that must not read as "selection changed".
        const stillOffered =
            contract !== null &&
            contract !== undefined &&
            contracts.some((c) => c.id === contract.id);
        if (stillOffered) return;
        onSetContract(pickDefaultContract(contracts));
    }, [contracts, contract, onSetContract]);

    // FIX: Reset the manual-edit flag when the payment method changes or the
    // cart is emptied so the field re-syncs to the new total automatically.
    useEffect(() => {
        amountManuallyEdited.current = false;
    }, [paymentMethod]);

    useEffect(() => {
        if (items.length === 0) {
            amountManuallyEdited.current = false;
        }
    }, [items.length]);

    // FIX: Always sync amountPaid to totals.total unless the user has manually
    // overridden the field. The old guard (`if (amountPaid === 0)`) caused the
    // amount to freeze after the first item was added.
    useEffect(() => {
        if ((paymentMethod === "cash" || paymentMethod === "split") && items.length > 0) {
            if (!amountManuallyEdited.current) {
                onSetAmountPaid(totals.total);
            }
        }
    }, [totals.total, paymentMethod, items.length, onSetAmountPaid]);

    const hasRxItems = items.some((i) => i.requiresPrescription);
    const isInsurance = paymentMethod === "insurance" || contract?.type === "insurance";
    const requiresRegisteredCustomer =
        isInsurance || contract?.type === "corporate" || contract?.type === "wholesale";
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
                                    {(() => {
                                        const drugAny = item.drug as unknown as Record<string, unknown>;
                                        const resolvedStock = stockQuantities[item.drug.id]
                                            ?? (typeof drugAny.available_quantity === "number" ? drugAny.available_quantity : undefined)
                                            ?? (typeof drugAny.valid_batch_quantity === "number" ? drugAny.valid_batch_quantity : undefined)
                                            ?? (typeof drugAny.quantity === "number" ? drugAny.quantity : undefined);
                                        const maxQty = resolvedStock ?? 1000;
                                        // One source of truth for "how does this
                                        // line read", shared with the input's clamp.
                                        const stockState = getLineStockState(
                                            item.quantity,
                                            resolvedStock
                                        );
                                        const atStockLimit =
                                            resolvedStock !== undefined &&
                                            item.quantity >= resolvedStock;
                                        return (
                                            <>
                                                <div className="flex items-center justify-between">
                                                    <div className="flex items-center bg-slate-50 border border-slate-200 rounded-lg overflow-hidden">
                                                        <button
                                                            onClick={() => {
                                                                const next = Math.max(1, item.quantity - 1);
                                                                onSetQuantity(item.drug.id, next);
                                                                // Keep any open draft in step with the cart, so a
                                                                // subsequent blur cannot resurrect a stale number.
                                                                setQtyDraft((d) => ({
                                                                    ...d,
                                                                    [item.drug.id]: String(next),
                                                                }));
                                                            }}
                                                            type="button"
                                                            aria-label={`Decrease quantity for ${item.drug.name}`}
                                                            className="w-8 h-8 flex items-center justify-center text-slate-500 hover:text-ink hover:bg-slate-100 transition-colors"
                                                        >
                                                            <Minus className="w-3 h-3" />
                                                        </button>
                                                        <input
                                                            type="text"
                                                            inputMode="numeric"
                                                            data-testid={`qty-input-${item.drug.id}`}
                                                            aria-label={`Quantity for ${item.drug.name}`}
                                                            min={1}
                                                            max={maxQty}
                                                            value={
                                                                qtyFocused.current === item.drug.id
                                                                    ? (qtyDraft[item.drug.id] ?? String(item.quantity))
                                                                    : String(item.quantity)
                                                            }
                                                            onFocus={(e) => {
                                                                // (a) Select the whole number so typing replaces it
                                                                // instead of appending (133 into 1 -> 1133).
                                                                qtyFocused.current = item.drug.id;
                                                                setQtyDraft((d) => ({
                                                                    ...d,
                                                                    [item.drug.id]: String(item.quantity),
                                                                }));
                                                                // Synchronous: select() during focus is what
                                                                // makes typing replace rather than append.
                                                                e.target.select();
                                                                requestAnimationFrame(() => e.target.select());
                                                            }}
                                                            onChange={(e) => {
                                                                // (d) digits only, no leading zeros.
                                                                const clean = sanitiseDraft(e.target.value);
                                                                setQtyDraft((d) => ({ ...d, [item.drug.id]: clean }));
                                                                // (d) A valid value within available stock may commit as
                                                                // you type; empty/0/over-limit wait for blur or Enter.
                                                                const n = Number(clean);
                                                                const withinLimit =
                                                                    resolvedStock === undefined
                                                                        ? n >= 1 && n <= 1000
                                                                        : n >= 1 && n <= resolvedStock;
                                                                if (withinLimit && Number.isFinite(n)) {
                                                                    onSetQuantity(item.drug.id, n);
                                                                }
                                                            }}
                                                            onKeyDown={(e) => {
                                                                if (e.key === "Enter") {
                                                                    e.preventDefault();
                                                                    onSetQuantity(
                                                                        item.drug.id,
                                                                        commitDraft(
                                                                            sanitiseDraft(
                                                                                qtyDraft[item.drug.id] ??
                                                                                    String(item.quantity)
                                                                            ),
                                                                            resolvedStock
                                                                        )
                                                                    );
                                                                    qtyFocused.current = null;
                                                                    skipNextCommitRef.current = item.drug.id;
                                                                    setQtyDraft((d) => {
                                                                        const { [item.drug.id]: _drop, ...rest } = d;
                                                                        return rest;
                                                                    });
                                                                    (e.target as HTMLInputElement).blur();
                                                                } else if (e.key === "Escape") {
                                                                    // (e) Revert to the committed value. The blur that
                                                                    // follows must NOT commit the abandoned draft.
                                                                    e.preventDefault();
                                                                    qtyFocused.current = null;
                                                                    skipNextCommitRef.current = item.drug.id;
                                                                    setQtyDraft((d) => {
                                                                        const { [item.drug.id]: _drop, ...rest } = d;
                                                                        return rest;
                                                                    });
                                                                    (e.target as HTMLInputElement).blur();
                                                                }
                                                            }}
                                                            onBlur={() => {
                                                                if (skipNextCommitRef.current === item.drug.id) {
                                                                    skipNextCommitRef.current = null;
                                                                    return;
                                                                }
                                                                // (c) Commit on blur, including tab/click-away.
                                                                (() => {
                                                                    const raw = sanitiseDraft(
                                                                        qtyDraft[item.drug.id] ??
                                                                            String(item.quantity)
                                                                    );
                                                                    const committed = commitDraft(
                                                                        raw,
                                                                        resolvedStock
                                                                    );
                                                                    onSetQuantity(item.drug.id, committed);
                                                                    // Only the 1000 ceiling is silent-unexpected: a
                                                                    // clamp to available is already explained by the
                                                                    // "Max reached (N)" label right beside it.
                                                                    setCappedLines((prev) => {
                                                                        const next = { ...prev };
                                                                        if (
                                                                            resolvedStock === undefined &&
                                                                            Number(raw) > LINE_QUANTITY_CAP
                                                                        ) {
                                                                            next[item.drug.id] = true;
                                                                        } else {
                                                                            delete next[item.drug.id];
                                                                        }
                                                                        return next;
                                                                    });
                                                                })();
                                                                qtyFocused.current = null;
                                                                setQtyDraft((d) => {
                                                                    const { [item.drug.id]: _drop, ...rest } = d;
                                                                    return rest;
                                                                });
                                                            }}
                                                            className="w-12 h-8 text-center text-sm font-bold bg-white border-x border-slate-200 focus:outline-none focus:bg-white"
                                                        />
                                                        <button
                                                            onClick={() => {
                                                                const next = Math.min(item.quantity + 1, maxQty);
                                                                onSetQuantity(item.drug.id, next);
                                                                setQtyDraft((d) => ({
                                                                    ...d,
                                                                    [item.drug.id]: String(next),
                                                                }));
                                                            }}
                                                            type="button"
                                                            disabled={atStockLimit}
                                                            title={
                                                                atStockLimit
                                                                    ? `Only ${resolvedStock} available`
                                                                    : undefined
                                                            }
                                                            aria-label={
                                                                atStockLimit
                                                                    ? `Increase quantity for ${item.drug.name}, limit reached: only ${resolvedStock} available`
                                                                    : `Increase quantity for ${item.drug.name}`
                                                            }
                                                            className={`w-8 h-8 flex items-center justify-center hover:text-ink hover:bg-slate-100 transition-colors ${
                                                                atStockLimit
                                                                    ? "text-slate-300 cursor-not-allowed"
                                                                    : "text-slate-500"
                                                            }`}
                                                        >
                                                            <Plus className="w-3 h-3" />
                                                        </button>
                                                    </div>
                                                    {/* Stock state. aria-live so a
                                                        screen reader announces the
                                                        change when stock refreshes. */}
                                                    <span
                                                        aria-live="polite"
                                                        data-testid={`stock-label-${item.drug.id}`}
                                                        data-state={stockState}
                                                        className={`text-xs ml-1 whitespace-nowrap truncate tabular-nums ${stockLabelClass(stockState)}`}
                                                    >
                                                        {stockLabelText(stockState, resolvedStock)}
                                                    </span>

                                                    <button
                                                        onClick={() => onRemoveItem(item.drug.id)}
                                                        type="button"
                                                        className="text-xs text-slate-400 hover:text-red-500 transition-colors flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-red-50"
                                                    >
                                                        <Trash2 className="w-3 h-3" />
                                                        Remove
                                                    </button>
                                                </div>

                                                {cappedLines[item.drug.id] && (
                                                    <p
                                                        role="status"
                                                        data-testid={`qty-cap-note-${item.drug.id}`}
                                                        className="text-[11px] text-amber-600 mt-1"
                                                    >
                                                        Maximum {LINE_QUANTITY_CAP} per line
                                                    </p>
                                                )}
                                                {/* Stock warning */}
                                                {resolvedStock !== undefined && item.quantity > resolvedStock && (
                                                    <div className="flex items-center gap-2 mt-2.5 px-3 py-2 rounded-lg text-xs border bg-red-50 border-red-100 text-red-700">
                                                        <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                                                        <span className="flex-1 font-medium">
                                                            Only {resolvedStock} available (requested {item.quantity})
                                                        </span>
                                                    </div>
                                                )}
                                            </>
                                        );
                                    })()}

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
                                        // The visible SectionLabel is a sibling, not an
                                        // associated <label>, so this combobox had NO
                                        // accessible name — a screen reader announced
                                        // two anonymous dropdowns in the checkout form.
                                        aria-label="Price contract"
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
                                                {/*
                                                  (e) The placeholder is a dead end: picking it clears
                                                  the selection, which puts the cart back into
                                                  "Select a price contract". So it exists only when
                                                  there is nothing to select, and the rule above
                                                  guarantees a value otherwise.
                                                */}
                                                {contracts.length === 0 && (
                                                    <option value="">— Select contract —</option>
                                                )}
                                                {contracts.map((c) => (
                                                    <option key={c.id} value={c.id}>{c.display}</option>
                                                ))}
                                            </>
                                        )}
                                    </select>
                                    <ChevronDown className="absolute right-3 top-3 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                                </div>
                                {contractsIssue?.message && (
                                    <div
                                        role="alert"
                                        data-testid="contract-load-issue"
                                        className={`mt-1.5 flex items-start gap-2 rounded-md border px-2 py-1.5 text-[11px] leading-snug ${
                                            contractsIssue.kind === "error"
                                                ? "border-red-300 bg-red-50 text-red-800"
                                                : "border-amber-300 bg-amber-50 text-amber-900"
                                        }`}
                                    >
                                        <span className="flex-1">{contractsIssue.message}</span>
                                        {onRetryContracts && (
                                            <button
                                                type="button"
                                                onClick={onRetryContracts}
                                                data-testid="contract-retry"
                                                className="shrink-0 rounded border border-current px-1.5 py-0.5 font-semibold uppercase tracking-wide"
                                            >
                                                Retry
                                            </button>
                                        )}
                                    </div>
                                )}
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
                                    requireRegistered={requiresRegisteredCustomer}
                                    fieldError={fieldError("customer")}
                                />
                            </div>

                            {/* Prescription ID */}
                            {hasRxItems && (
                                <div>
                                    <SectionLabel icon={FileText}>Prescription</SectionLabel>
                                    <PrescriptionSelector
                                        customerId={customerId}
                                        rxItems={items.filter((item) => item.requiresPrescription)}
                                        prescriptionId={prescriptionId}
                                        error={fieldError("prescription_id")}
                                        onSetPrescriptionId={onSetPrescriptionId}
                                        onSetPrescriptionVerified={onSetPrescriptionVerified}
                                    />
                                </div>
                            )}

                            {/* Insurance */}
                            {isInsurance && (
                                <div className="space-y-2.5 p-3.5 rounded-xl bg-blue-50 border border-blue-100">
                                    <p className="text-[11px] font-bold text-blue-700 uppercase tracking-widest">
                                        Insurance Details
                                    </p>
                                    {/* Patient copay display */}
                                    <div className="flex items-center justify-between text-sm font-semibold text-blue-700">
                                        <span>Patient copay</span>
                                        <span>₵{totals.patientCopay.toFixed(2)}</span>
                                    </div>
                                    <div className="flex gap-2">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                onSetPaymentMethod("insurance");
                                                onSetAmountPaid(totals.patientCopay);
                                            }}
                                            className="py-1 px-2 rounded bg-white border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50"
                                        >
                                            Set as Amount Paid
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const copay = totals.patientCopay;
                                                const insuranceShare = Math.max(0, totals.total - copay);
                                                onSetPaymentMethod("split");
                                                onSetSplitPayment({ insurance: insuranceShare, cash: copay });
                                            }}
                                            className="py-1 px-2 rounded bg-white border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50"
                                        >
                                            Apply as Split
                                        </button>
                                    </div>
                                    {(totals.patientCopay === 0 || (amountPaid > 0 && paymentMethod === "insurance")) && (
                                        <div className="flex items-center justify-between text-xs font-semibold text-blue-700 bg-white/70 border border-blue-100 rounded-lg px-2.5 py-2">
                                            <span>{totals.patientCopay === 0 ? "No patient copay due" : "Amount paid set"}</span>
                                            <span>₵{(totals.patientCopay === 0 ? 0 : amountPaid).toFixed(2)}</span>
                                        </div>
                                    )}
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
                                        <span className="text-sm font-semibold text-blue-700">
                                            Card verified ✓
                                        </span>
                                    </label>
                                    {fieldError("insurance") && <p className="text-xs text-red-500">{fieldError("insurance")}</p>}
                                </div>
                            )}

                            {/* Payment method */}
                            <div>
                                <SectionLabel icon={Banknote}>Payment Method</SectionLabel>
                                <div className="relative">
                                    <select
                                        value={paymentMethod}
                                        onChange={(e) => onSetPaymentMethod(e.target.value as PaymentMethod)}
                                        className={`${inputCls} appearance-none pr-8 ${fieldError("payment_method") ? "border-red-300 bg-red-50/30" : ""}`}
                                    >
                                        {PAYMENT_METHODS.map((m) => (
                                            <option key={m.value} value={m.value}>{m.label}</option>
                                        ))}
                                    </select>
                                    <ChevronDown className="absolute right-3 top-3 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                                </div>
                                {fieldError("payment_method") && (
                                    <p className="text-xs text-red-500 mt-1.5 flex items-center gap-1">
                                        <AlertCircle className="w-3 h-3" />{fieldError("payment_method")}
                                    </p>
                                )}
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
                                            value={amountPaid ?? ""}
                                            onChange={(e) => {
                                                // FIX: Mark as manually edited so the auto-sync effect
                                                // stops overwriting what the cashier has typed.
                                                amountManuallyEdited.current = true;
                                                onSetAmountPaid(parseFloat(e.target.value) || 0);
                                            }}
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
                                <span>Tax{taxInclusive ? " (incl.)" : ""}</span>
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
                    {checkoutError && (
                        <div
                            role="alert"
                            className="rounded-xl bg-red-50 border border-red-200 px-3 py-2.5 mb-3 text-xs text-red-700 flex items-start gap-1.5"
                        >
                            <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                            {checkoutError}
                        </div>
                    )}

                    {/* CTA */}
                    <button
                        onClick={onCheckout}
                        // Block checkout while the cart is invalid — expired/
                        // insufficient batches, unverified prescriptions, short
                        // payment. These are already computed and shown above;
                        // leaving the CTA live let a cashier submit a sale the
                        // validator had just rejected.
                        disabled={isSubmitting || validationErrors.length > 0}
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
