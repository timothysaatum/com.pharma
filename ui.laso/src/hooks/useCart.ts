/**
 * useCart.ts
 * ==========
 * Central state for the POS cart. Manages:
 *   - Line items (add, remove, update quantity)
 *   - Selected price contract
 *   - Customer (walk-in name or registered id)
 *   - Payment method and amount paid
 *   - Derived totals (computed from contract + items)
 *   - Prescription and insurance state
 *
 * Totals are ESTIMATED client-side for display only.
 * The server is the source of truth — ProcessSaleResponse.sale.total_amount
 * is what actually gets charged. Discrepancies are surfaced in warnings[].
 *
 * The hook exposes `buildSaleCreate()` which assembles the exact
 * SaleCreate payload that POST /sales/ expects.
 */

import { useReducer, useCallback, useMemo } from "react";
import type { Drug } from "@/types";
import type { AvailableContract } from "@/api/contracts";
import type { SaleCreate } from "@/types";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface CartItem {
    drug: Drug;
    quantity: number;
    /** Set when the cashier confirms this item requires a prescription */
    requiresPrescription: boolean;
    prescriptionVerified: boolean;
    /** Optionally pin to a specific batch (FEFO used if null) */
    batchId: string | null;
}

export type PaymentMethod =
    | "cash"
    | "card"
    | "mobile_money"
    | "insurance"
    | "credit"
    | "split";

export interface SplitPayment {
    cash: number;
    card: number;
    mobile_money: number;
    insurance: number;
    credit: number;
}

export interface CartState {
    items: CartItem[];
    contract: AvailableContract | null;
    /** Walk-in name — used when customerId is null */
    customerName: string;
    customerId: string | null;
    paymentMethod: PaymentMethod;
    amountPaid: number;
    splitPayment: Partial<SplitPayment>;
    prescriptionId: string | null;
    insuranceClaimNumber: string;
    insurancePreAuthNumber: string;
    insuranceVerified: boolean;
    notes: string;
}

// ── Actions ───────────────────────────────────────────────────────────────────

type CartAction =
    | { type: "ADD_ITEM"; drug: Drug }
    | { type: "REMOVE_ITEM"; drugId: string }
    | { type: "SET_QUANTITY"; drugId: string; quantity: number }
    | { type: "SET_PRESCRIPTION_VERIFIED"; drugId: string; verified: boolean }
    | { type: "SET_BATCH"; drugId: string; batchId: string | null }
    | { type: "SET_CONTRACT"; contract: AvailableContract | null }
    | { type: "SET_CUSTOMER_NAME"; name: string }
    | { type: "SET_CUSTOMER_ID"; id: string | null }
    | { type: "SET_PAYMENT_METHOD"; method: PaymentMethod }
    | { type: "SET_AMOUNT_PAID"; amount: number }
    | { type: "SET_SPLIT_PAYMENT"; split: Partial<SplitPayment> }
    | { type: "SET_PRESCRIPTION_ID"; id: string | null }
    | { type: "SET_INSURANCE_CLAIM"; number: string }
    | { type: "SET_INSURANCE_PREAUTH"; number: string }
    | { type: "SET_INSURANCE_VERIFIED"; verified: boolean }
    | { type: "SET_NOTES"; notes: string }
    | { type: "CLEAR_CART" };

// ── Initial state ─────────────────────────────────────────────────────────────

const INITIAL_STATE: CartState = {
    items: [],
    contract: null,
    customerName: "",
    customerId: null,
    paymentMethod: "cash",
    amountPaid: 0,
    splitPayment: {},
    prescriptionId: null,
    insuranceClaimNumber: "",
    insurancePreAuthNumber: "",
    insuranceVerified: false,
    notes: "",
};

// ── Reducer ───────────────────────────────────────────────────────────────────

function cartReducer(state: CartState, action: CartAction): CartState {
    switch (action.type) {
        case "ADD_ITEM": {
            const exists = state.items.find((i) => i.drug.id === action.drug.id);
            if (exists) {
                // Increment quantity if already in cart
                return {
                    ...state,
                    items: state.items.map((i) =>
                        i.drug.id === action.drug.id
                            ? { ...i, quantity: i.quantity + 1 }
                            : i
                    ),
                };
            }
            return {
                ...state,
                items: [
                    ...state.items,
                    {
                        drug: action.drug,
                        quantity: 1,
                        requiresPrescription: action.drug.requires_prescription,
                        prescriptionVerified: false,
                        batchId: null,
                    },
                ],
            };
        }

        case "REMOVE_ITEM":
            return {
                ...state,
                items: state.items.filter((i) => i.drug.id !== action.drugId),
            };

        case "SET_QUANTITY": {
            if (action.quantity <= 0) {
                return {
                    ...state,
                    items: state.items.filter((i) => i.drug.id !== action.drugId),
                };
            }
            // Cap at 1000 per item (matches SaleItemCreate.quantity le=1000)
            const qty = Math.min(action.quantity, 1000);
            return {
                ...state,
                items: state.items.map((i) =>
                    i.drug.id === action.drugId ? { ...i, quantity: qty } : i
                ),
            };
        }

        case "SET_PRESCRIPTION_VERIFIED":
            return {
                ...state,
                items: state.items.map((i) =>
                    i.drug.id === action.drugId
                        ? { ...i, prescriptionVerified: action.verified }
                        : i
                ),
            };

        case "SET_BATCH":
            return {
                ...state,
                items: state.items.map((i) =>
                    i.drug.id === action.drugId
                        ? { ...i, batchId: action.batchId }
                        : i
                ),
            };

        case "SET_CONTRACT":
            return {
                ...state,
                contract: action.contract,
                // Reset insurance state when switching away from insurance contract
                insuranceVerified: action.contract?.type === "insurance"
                    ? state.insuranceVerified
                    : false,
                insuranceClaimNumber: action.contract?.type === "insurance"
                    ? state.insuranceClaimNumber
                    : "",
            };

        case "SET_CUSTOMER_NAME":
            return { ...state, customerName: action.name, customerId: null };

        case "SET_CUSTOMER_ID":
            return { ...state, customerId: action.id };

        case "SET_PAYMENT_METHOD":
            return {
                ...state,
                paymentMethod: action.method,
                splitPayment: action.method === "split" ? state.splitPayment : {},
            };

        case "SET_AMOUNT_PAID":
            return { ...state, amountPaid: action.amount };

        case "SET_SPLIT_PAYMENT":
            return { ...state, splitPayment: action.split };

        case "SET_PRESCRIPTION_ID":
            return { ...state, prescriptionId: action.id };

        case "SET_INSURANCE_CLAIM":
            return { ...state, insuranceClaimNumber: action.number };

        case "SET_INSURANCE_PREAUTH":
            return { ...state, insurancePreAuthNumber: action.number };

        case "SET_INSURANCE_VERIFIED":
            return { ...state, insuranceVerified: action.verified };

        case "SET_NOTES":
            return { ...state, notes: action.notes };

        case "CLEAR_CART":
            return INITIAL_STATE;

        default:
            return state;
    }
}

// ── Derived totals ────────────────────────────────────────────────────────────

export interface CartTotals {
    subtotal: number;
    discountAmount: number;
    taxAmount: number;
    total: number;
    change: number;
    itemCount: number;
}

function computeTotals(state: CartState): CartTotals {
    const subtotal = state.items.reduce(
        (sum, item) => sum + item.drug.unit_price * item.quantity,
        0
    );

    // Estimate discount from selected contract
    const discountPct = state.contract?.discount_percentage ?? 0;
    const discountAmount = parseFloat(
        ((subtotal * discountPct) / 100).toFixed(2)
    );

    // Estimate tax from first item's tax rate (simplified client-side estimate)
    // Server recalculates per-item accurately
    const avgTaxRate =
        state.items.length > 0
            ? state.items.reduce((sum, i) => sum + i.drug.tax_rate, 0) /
            state.items.length
            : 0;
    const taxableAmount = subtotal - discountAmount;
    const taxAmount = parseFloat(
        ((taxableAmount * avgTaxRate) / 100).toFixed(2)
    );

    const total = parseFloat((taxableAmount + taxAmount).toFixed(2));
    const change = Math.max(0, parseFloat((state.amountPaid - total).toFixed(2)));
    const itemCount = state.items.reduce((sum, i) => sum + i.quantity, 0);

    return { subtotal, discountAmount, taxAmount, total, change, itemCount };
}

// ── Validation ────────────────────────────────────────────────────────────────

export interface CartValidationError {
    field: string;
    message: string;
}

function validateCart(
    state: CartState,
    totals: CartTotals
): CartValidationError[] {
    const errors: CartValidationError[] = [];

    if (state.items.length === 0) {
        errors.push({ field: "items", message: "Cart is empty" });
    }

    if (!state.contract) {
        errors.push({ field: "contract", message: "Select a price contract" });
    }

    if (!state.customerId && !state.customerName.trim()) {
        errors.push({
            field: "customer",
            message: "Enter customer name or select a registered customer",
        });
    }

    // Insurance validations
    if (state.paymentMethod === "insurance") {
        if (!state.customerId) {
            errors.push({
                field: "customer",
                message: "Registered customer required for insurance payment",
            });
        }
        if (!state.insuranceVerified) {
            errors.push({
                field: "insurance",
                message: "Insurance must be verified before processing",
            });
        }
        if (!state.insuranceClaimNumber.trim()) {
            errors.push({
                field: "insurance_claim",
                message: "Insurance claim number is required",
            });
        }
    }

    // Prescription validations
    const hasRxItems = state.items.some((i) => i.requiresPrescription);
    if (hasRxItems) {
        const unverified = state.items.filter(
            (i) => i.requiresPrescription && !i.prescriptionVerified
        );
        if (unverified.length > 0) {
            errors.push({
                field: "prescription",
                message: `Verify prescription for: ${unverified
                    .map((i) => i.drug.name)
                    .join(", ")}`,
            });
        }
        if (!state.prescriptionId) {
            errors.push({
                field: "prescription_id",
                message: "Prescription ID required for prescription drugs",
            });
        }
    }

    // Cash payment — check amount paid covers total
    if (
        state.paymentMethod === "cash" &&
        state.amountPaid > 0 &&
        state.amountPaid < totals.total
    ) {
        errors.push({
            field: "amount_paid",
            message: `Amount paid (₵${state.amountPaid.toFixed(2)}) is less than total (₵${totals.total.toFixed(2)})`,
        });
    }

    // Split payment — must have at least 2 methods
    if (state.paymentMethod === "split") {
        const splitEntries = Object.entries(state.splitPayment).filter(
            ([, v]) => v && v > 0
        );
        if (splitEntries.length < 2) {
            errors.push({
                field: "split_payment",
                message: "Split payment requires at least 2 payment methods",
            });
        }
    }

    return errors;
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useCart() {
    const [state, dispatch] = useReducer(cartReducer, INITIAL_STATE);

    const totals = useMemo(() => computeTotals(state), [state]);

    const validationErrors = useMemo(
        () => validateCart(state, totals),
        [state, totals]
    );

    const isValid = validationErrors.length === 0;

    /**
     * Build the exact SaleCreate payload for POST /sales/.
     * Only call this after isValid === true.
     */
    const buildSaleCreate = useCallback(
        (branchId: string): SaleCreate => {
            if (!state.contract) throw new Error("No contract selected");

            const splitDetails =
                state.paymentMethod === "split" &&
                    Object.keys(state.splitPayment).length > 0
                    ? (state.splitPayment as Record<string, number>)
                    : undefined;

            return {
                branch_id: branchId,
                price_contract_id: state.contract.id,
                customer_id: state.customerId ?? undefined,
                customer_name: state.customerId
                    ? undefined
                    : state.customerName.trim() || undefined,
                items: state.items.map((item) => ({
                    drug_id: item.drug.id,
                    quantity: item.quantity,
                    batch_id: item.batchId ?? undefined,
                    requires_prescription: item.requiresPrescription,
                    prescription_verified: item.prescriptionVerified,
                })),
                payment_method: state.paymentMethod,
                amount_paid: state.amountPaid || undefined,
                payment_reference: undefined,
                split_payment_details: splitDetails,
                prescription_id: state.prescriptionId ?? undefined,
                insurance_verified: state.insuranceVerified,
                insurance_claim_number:
                    state.insuranceClaimNumber.trim() || undefined,
                insurance_preauth_number:
                    state.insurancePreAuthNumber.trim() || undefined,
                notes: state.notes.trim() || undefined,
            } as SaleCreate;
        },
        [state]
    );

    return {
        state,
        totals,
        validationErrors,
        isValid,
        buildSaleCreate,

        // Actions
        addItem: useCallback(
            (drug: Drug) => dispatch({ type: "ADD_ITEM", drug }),
            []
        ),
        removeItem: useCallback(
            (drugId: string) => dispatch({ type: "REMOVE_ITEM", drugId }),
            []
        ),
        setQuantity: useCallback(
            (drugId: string, quantity: number) =>
                dispatch({ type: "SET_QUANTITY", drugId, quantity }),
            []
        ),
        setPrescriptionVerified: useCallback(
            (drugId: string, verified: boolean) =>
                dispatch({ type: "SET_PRESCRIPTION_VERIFIED", drugId, verified }),
            []
        ),
        setBatch: useCallback(
            (drugId: string, batchId: string | null) =>
                dispatch({ type: "SET_BATCH", drugId, batchId }),
            []
        ),
        setContract: useCallback(
            (contract: AvailableContract | null) =>
                dispatch({ type: "SET_CONTRACT", contract }),
            []
        ),
        setCustomerName: useCallback(
            (name: string) => dispatch({ type: "SET_CUSTOMER_NAME", name }),
            []
        ),
        setCustomerId: useCallback(
            (id: string | null) => dispatch({ type: "SET_CUSTOMER_ID", id }),
            []
        ),
        setPaymentMethod: useCallback(
            (method: PaymentMethod) =>
                dispatch({ type: "SET_PAYMENT_METHOD", method }),
            []
        ),
        setAmountPaid: useCallback(
            (amount: number) => dispatch({ type: "SET_AMOUNT_PAID", amount }),
            []
        ),
        setSplitPayment: useCallback(
            (split: Partial<SplitPayment>) =>
                dispatch({ type: "SET_SPLIT_PAYMENT", split }),
            []
        ),
        setPrescriptionId: useCallback(
            (id: string | null) => dispatch({ type: "SET_PRESCRIPTION_ID", id }),
            []
        ),
        setInsuranceClaimNumber: useCallback(
            (number: string) => dispatch({ type: "SET_INSURANCE_CLAIM", number }),
            []
        ),
        setInsurancePreAuthNumber: useCallback(
            (number: string) =>
                dispatch({ type: "SET_INSURANCE_PREAUTH", number }),
            []
        ),
        setInsuranceVerified: useCallback(
            (verified: boolean) =>
                dispatch({ type: "SET_INSURANCE_VERIFIED", verified }),
            []
        ),
        setNotes: useCallback(
            (notes: string) => dispatch({ type: "SET_NOTES", notes }),
            []
        ),
        clearCart: useCallback(() => dispatch({ type: "CLEAR_CART" }), []),
    };
}