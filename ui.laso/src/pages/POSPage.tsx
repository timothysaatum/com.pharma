/**
 * ===========
 * Main Point of Sale screen. Composed of:
 *   - DrugSearchPanel (left) — search and add to cart
 *   - CartPanel (right) — cart, contract, payment, checkout
 *   - SaleSuccessModal — post-sale confirmation
 *
 * Flow:
 *   1. Cashier searches and adds drugs to cart
 *   2. Selects price contract (auto-selected to default)
 *   3. Enters customer name or selects registered customer
 *   4. Chooses payment method and amount
 *   5. Hits "Complete Sale" → POST /sales/
 *   6. Success modal shows change, loyalty points, warnings
 *   7. "New Sale" clears cart and resets state
 *
 * Offline:
 *   When offline, the sale is written to local SQLite via writeLocal.sale()
 *   and queued for sync. The UI shows a banner indicating offline mode.
 *   Contract list is read from local price_contracts table.
 *
 * Security:
 *   - Only users with process_sales permission reach this page (RequireAuth)
 *   - Contract list is filtered server-side by role and branch
 *   - Prescription items require explicit verification checkbox per item
 *   - Insurance payment requires verified checkbox + claim number
 *   - All financial calculations are server-confirmed in ProcessSaleResponse
 */

import { useState, useEffect, useCallback } from "react";
import { AnimatePresence } from "framer-motion";
import { WifiOff, AlertTriangle } from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { contractsApi, type AvailableContract } from "@/api/contracts";
import { salesApi, type ProcessSaleResponse } from "@/api/sales";
import { parseApiError } from "@/api/client";
import { writeLocal } from "@/lib/localWrite";
import { appEvents } from "@/lib/events";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { useCart } from "@/hooks/useCart";
import { DrugSearchPanel } from "@/components/pos/DrugSearchPanel";
import { CartPanel } from "@/components/pos/CartPanel";
import { SaleSuccessModal } from "@/components/pos/SaleSuccessModal";


export default function POSPage() {
    const { user, activeBranchId } = useAuthStore();
    const { status: syncStatus } = useSyncStatus();
    const isOffline = syncStatus === "offline";

    // ── Cart state ─────────────────────────────────────────────────────────────
    const cart = useCart();

    // ── Contracts ──────────────────────────────────────────────────────────────
    const [contracts, setContracts] = useState<AvailableContract[]>([]);
    const [contractsLoading, setContractsLoading] = useState(true);

    const loadContracts = useCallback(async () => {
        if (!activeBranchId) return;
        setContractsLoading(true);
        try {
            const data = await contractsApi.getAvailableForPos(activeBranchId);
            setContracts(data);
        } catch {
            // Non-blocking — cashier can still proceed with empty list
            setContracts([]);
        } finally {
            setContractsLoading(false);
        }
    }, [activeBranchId]);

    useEffect(() => {
        loadContracts();
    }, [loadContracts]);

    // ── Checkout ───────────────────────────────────────────────────────────────
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [checkoutError, setCheckoutError] = useState<string | null>(null);
    const [successResult, setSuccessResult] = useState<ProcessSaleResponse | null>(null);

    const handleCheckout = useCallback(async () => {
        if (!cart.isValid || !activeBranchId) return;

        setIsSubmitting(true);
        setCheckoutError(null);

        const payload = cart.buildSaleCreate(activeBranchId);

        try {
            let result: ProcessSaleResponse;

            if (isOffline) {
                // ── Offline path ─────────────────────────────────────────────
                // Build a local sale record and queue it for sync.
                // We construct a synthetic ProcessSaleResponse so the UI flow
                // is identical — the "sale" object uses client-computed totals.
                const saleId = crypto.randomUUID();
                const saleNumber = `OFFLINE-${Date.now()}`;
                const now = new Date().toISOString();
                const totals = cart.totals;

                const offlineSale = {
                    id: saleId,
                    sale_number: saleNumber,
                    organization_id: user?.organization_id ?? "",
                    branch_id: activeBranchId,
                    customer_id: payload.customer_id ?? null,
                    customer_name: payload.customer_name ?? null,
                    subtotal: totals.subtotal,
                    discount_amount: totals.discountAmount,
                    contract_discount_amount: totals.discountAmount,
                    additional_discount_amount: 0,
                    total_discount_amount: totals.discountAmount,
                    tax_amount: totals.taxAmount,
                    total_amount: totals.total,
                    price_contract_id: payload.price_contract_id,
                    contract_name: cart.state.contract?.name ?? null,
                    contract_type: cart.state.contract?.type ?? null,
                    contract_discount_percentage: cart.state.contract?.discount_percentage ?? 0,
                    payment_method: payload.payment_method,
                    payment_status: "completed" as const,
                    amount_paid: payload.amount_paid ?? totals.total,
                    change_amount: Math.max(0, (payload.amount_paid ?? 0) - totals.total),
                    payment_reference: payload.payment_reference ?? null,
                    split_payment_details: null,
                    prescription_id: payload.prescription_id ?? null,
                    prescription_number: null,
                    prescriber_name: null,
                    cashier_id: user?.id ?? "",
                    pharmacist_id: null,
                    insurance_claim_number: payload.insurance_claim_number ?? null,
                    insurance_preauth_number: null,
                    patient_copay_amount: null,
                    insurance_covered_amount: null,
                    insurance_verified: payload.insurance_verified,
                    insurance_verified_at: null,
                    insurance_verified_by: null,
                    notes: payload.notes ?? null,
                    status: "completed" as const,
                    cancelled_at: null,
                    cancelled_by: null,
                    cancellation_reason: null,
                    refund_amount: null,
                    refunded_at: null,
                    receipt_printed: false,
                    receipt_emailed: false,
                    sync_status: "pending" as const,
                    sync_version: 1,
                    synced_at: null,
                    updated_at: now,
                    created_at: now,
                    items: cart.state.items.map((item) => {
                        const lineSubtotal = item.drug.unit_price * item.quantity;
                        const discountPct = cart.state.contract?.discount_percentage ?? 0;
                        const contractDiscountAmt = parseFloat(((lineSubtotal * discountPct) / 100).toFixed(2));
                        const taxAmt = parseFloat(((lineSubtotal * item.drug.tax_rate) / 100).toFixed(2));
                        return {
                            id: crypto.randomUUID(),
                            sale_id: saleId,
                            drug_id: item.drug.id,
                            drug_name: item.drug.name,
                            drug_sku: item.drug.sku,
                            drug_generic_name: item.drug.generic_name,
                            quantity: item.quantity,
                            batch_id: item.batchId,
                            batch_number: null,
                            batch_expiry_date: null,
                            unit_price: item.drug.unit_price,
                            subtotal: lineSubtotal,
                            discount_percentage: discountPct,
                            discount_amount: contractDiscountAmt,
                            contract_discount_percentage: discountPct,
                            contract_discount_amount: contractDiscountAmt,
                            additional_discount_amount: 0,
                            total_discount_amount: contractDiscountAmt,
                            tax_rate: item.drug.tax_rate,
                            tax_amount: taxAmt,
                            total_price: lineSubtotal - contractDiscountAmt + taxAmt,
                            applied_contract_id: payload.price_contract_id,
                            applied_contract_name: cart.state.contract?.name ?? null,
                            insurance_covered: false,
                            patient_copay: null,
                            requires_prescription: item.requiresPrescription,
                            prescription_verified: item.prescriptionVerified,
                            prescription_id: null,
                            allergy_check_performed: false,
                            created_at: now,
                            updated_at: now,
                        };
                    }),
                };

                // The offline object is a superset of what writeLocal needs
                await writeLocal.sale(offlineSale as unknown as Parameters<typeof writeLocal.sale>[0]);

                // Update local inventory for each item
                for (const item of cart.state.items) {
                    await writeLocal.inventory(activeBranchId, item.drug.id, -item.quantity);
                }

                result = {
                    sale: {
                        ...offlineSale,
                        // SaleWithDetails extra fields
                        customer_full_name: payload.customer_name ?? null,
                        customer_phone: null,
                        customer_email: null,
                        customer_loyalty_tier: null,
                        branch_name: "",
                        branch_address: null,
                        organization_name: "",
                        organization_tax_id: null,
                        cashier_name: user?.full_name ?? null,
                        pharmacist_name: null,
                        manager_approval_user_id: null,
                        insurance_preauth_number: null,
                    },
                    inventory_updated: cart.state.items.length,
                    batches_updated: 0,
                    loyalty_points_awarded: 0,
                    loyalty_tier_upgraded: false,
                    new_loyalty_tier: null,
                    low_stock_alerts_created: 0,
                    expiry_alerts_created: 0,
                    contract_applied: cart.state.contract?.name ?? "Standard",
                    contract_discount_given: totals.discountAmount,
                    estimated_savings: totals.discountAmount,
                    success: true,
                    message: "Sale recorded offline — will sync when connection is restored",
                    warnings: ["⚠ Sale recorded offline and will be synced when back online"],
                };
            } else {
                // ── Online path ──────────────────────────────────────────────
                result = await salesApi.processSale(payload);
            }

            setSuccessResult(result);
            // Notify inventory page to refresh its counts
            appEvents.emit("inventory:changed");
            appEvents.emit("sales:changed");
        } catch (err) {
            setCheckoutError(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    }, [cart, activeBranchId, isOffline, user]);

    const handleNewSale = useCallback(() => {
        setSuccessResult(null);
        setCheckoutError(null);
        cart.clearCart();
    }, [cart]);

    // Drug IDs already in cart — used to show "In cart" state in search panel
    const cartDrugIds = new Set(cart.state.items.map((i) => i.drug.id));

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between flex-shrink-0">
                <div>
                    <h1 className="font-display text-2xl font-bold text-ink">Point of Sale</h1>
                    <p className="text-sm text-ink-muted mt-0.5">
                        {user?.full_name} · {user?.role}
                    </p>
                </div>
                <div className="flex items-center gap-3">
                    {isOffline && (
                        <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-50 border border-amber-200 rounded-xl text-amber-700 text-sm font-medium">
                            <WifiOff className="w-4 h-4" />
                            Offline mode — sales will sync when back online
                        </div>
                    )}
                </div>
            </div>

            {/* Checkout error banner */}
            {checkoutError && (
                <div className="mx-6 mt-3 rounded-xl bg-red-50 border border-red-100 p-3 flex gap-2 items-start flex-shrink-0">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                    <p className="text-sm text-red-600">{checkoutError}</p>
                    <button
                        onClick={() => setCheckoutError(null)}
                        className="ml-auto text-ink-muted hover:text-ink flex-shrink-0"
                    >
                        ×
                    </button>
                </div>
            )}

            {/* Two-panel layout */}
            <div className="flex flex-1 min-h-0 overflow-hidden">
                {/* Left: Drug search — takes remaining space */}
                <div className="flex-1 border-r border-slate-200 flex flex-col min-h-0">
                    <DrugSearchPanel
                        onAdd={cart.addItem}
                        disabledDrugIds={cartDrugIds}
                    />
                </div>

                {/* Right: Cart — fixed width flush to screen edge */}
                <div className="w-[38%] flex-shrink-0 flex flex-col min-h-0 overflow-hidden">
                    <CartPanel
                        items={cart.state.items}
                        contract={cart.state.contract}
                        contracts={contracts}
                        contractsLoading={contractsLoading}
                        customerName={cart.state.customerName}
                        customerId={cart.state.customerId}
                        paymentMethod={cart.state.paymentMethod}
                        amountPaid={cart.state.amountPaid}
                        prescriptionId={cart.state.prescriptionId}
                        insuranceClaimNumber={cart.state.insuranceClaimNumber}
                        insurancePreAuthNumber={cart.state.insurancePreAuthNumber}
                        insuranceVerified={cart.state.insuranceVerified}
                        notes={cart.state.notes}
                        totals={cart.totals}
                        validationErrors={cart.validationErrors}
                        isSubmitting={isSubmitting}
                        onSetQuantity={cart.setQuantity}
                        onRemoveItem={cart.removeItem}
                        onSetPrescriptionVerified={cart.setPrescriptionVerified}
                        onSetContract={cart.setContract}
                        onSetCustomerId={cart.setCustomerId}
                        onSetCustomerName={cart.setCustomerName}
                        onSetPaymentMethod={cart.setPaymentMethod}
                        onSetAmountPaid={cart.setAmountPaid}
                        onSetPrescriptionId={cart.setPrescriptionId}
                        onSetInsuranceClaimNumber={cart.setInsuranceClaimNumber}
                        onSetInsurancePreAuthNumber={cart.setInsurancePreAuthNumber}
                        onSetInsuranceVerified={cart.setInsuranceVerified}
                        onSetNotes={cart.setNotes}
                        onCheckout={handleCheckout}
                        onClearCart={cart.clearCart}
                    />
                </div>
            </div>

            {/* Success modal */}
            <AnimatePresence>
                {successResult && (
                    <SaleSuccessModal
                        result={successResult}
                        onNewSale={handleNewSale}
                        onClose={handleNewSale}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}