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

import { useState, useEffect, useCallback, useRef } from "react";
import { AnimatePresence } from "framer-motion";
import { useAuthStore } from "@/stores/authStore";
import { contractsApi, type AvailableContract } from "@/api/contracts";
import { salesApi, type ProcessSaleResponse } from "@/api/sales";
import { statsApi } from "@/api/stats";
import { isBackendKnownUnreachable, isBackendReachable, isOfflineError, parseApiError } from "@/api/client";
import { toast } from "sonner";
import { offlineSalesManager } from "@/lib/offlineSalesManager";
import {
    shouldFallbackToOfflineSaleAfterError,
    shouldUseOfflineSalePath,
} from "@/lib/offlineFallback";
import { appEvents, useAppEvent } from "@/lib/events";
import type { Drug } from "@/types";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import { useCart } from "@/hooks/useCart";
import { useOrganization } from "@/hooks/useOrganization";
import { localRead } from "@/lib/localRead";
import { DrugSearchPanel } from "@/components/pos/DrugSearchPanel";
import { CartPanel } from "@/components/pos/CartPanel";
import { SaleSuccessModal } from "@/components/pos/SaleSuccessModal";

interface CheckoutIntent {
    saleId: string;
    saleNumber: string;
    idempotencyKey: string;
}

export default function POSPage() {
    const { user, activeBranchId } = useAuthStore();
    const { status: syncStatus } = useSyncStatus();
    const isOffline = syncStatus === "offline";

    // ── Daily Sales state ──────────────────────────────────────────────────────
    const [todaySales, setTodaySales] = useState<number>(0);

    const fetchTodaySales = useCallback(async () => {
        if (!activeBranchId) return;

        try {
            if (navigator.onLine && !isOffline && !isBackendKnownUnreachable()) {
                const stats = await statsApi.getBranchStats(activeBranchId);
                setTodaySales(stats.total_sales_today);
            } else {
                // Offline fallback: calculate from local database
                const today = new Date().toISOString().slice(0, 10);
                const localSales = await localRead.searchSales({
                    branch_id: activeBranchId,
                    start_date: today,
                    end_date: today,
                    status: "completed",
                    page_size: 1000 // Assume no more than 1000 sales per day for offline calculation
                });
                const total = localSales.items.reduce((sum, s) => sum + Number(s.total_amount || 0), 0);
                setTodaySales(total);
            }
        } catch (error) {
            toast.error(parseApiError(error));
        }
    }, [activeBranchId, isOffline]);

    useEffect(() => {
        fetchTodaySales();
    }, [fetchTodaySales]);

    useAppEvent("sales:changed", fetchTodaySales);

    // ── Organization settings ──────────────────────────────────────────────────
    const { org: posOrg } = useOrganization();
    const settings = (posOrg?.settings ?? {}) as Record<string, unknown>;
    const taxInclusive = Boolean(settings.tax_inclusive);
    const footerMessage = (settings.receipt_footer as string) ?? "";

    // ── Cart state ─────────────────────────────────────────────────────────────
    const cart = useCart(taxInclusive);

    // ── Stock validation ─────────────────────────────────────────────────────
    const [stockQuantities, setStockQuantities] = useState<Record<string, number>>({});
    const stockRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const refreshStock = useCallback(async (branchId: string, items: Array<{ drug: Drug; quantity: number }>) => {
        try {
            const sq: Record<string, number> = {};
            const errors: Record<string, string> = {};
            for (const item of items) {
                const info = await localRead.getSellableQuantity(branchId, item.drug.id);
                if (info.notStocked) {
                    errors[item.drug.id] = `${item.drug.name} is not stocked at the active branch.`;
                    sq[item.drug.id] = 0;
                } else {
                    sq[item.drug.id] = info.sellable;
                    if (info.sellable <= 0) {
                        errors[item.drug.id] = info.noBatchData
                            ? `${item.drug.name} — stock details not synced to this device yet. Sync to sell.`
                            : `${item.drug.name} — No valid non-expired batches. Cannot sell.`;
                    } else if (item.quantity > info.sellable) {
                        errors[item.drug.id] = `Insufficient stock for ${item.drug.name}. Requested ${item.quantity}, available ${info.sellable}.`;
                    }
                }
            }
            setStockQuantities(sq);
            cart.setStockQuantities(sq, errors);
        } catch {
            // Stock validation unavailable — fail open is safer than blocking sales
            // due to a local DB error, but setStockQuantities will mark items as
            // "unloaded" in validateCart so the user sees a clear message.
            cart.setStockQuantities({}, {});
        }
    }, [cart.setStockQuantities]);

    // Refresh stock whenever cart items change or branch changes
    useEffect(() => {
        if (stockRefreshRef.current) clearTimeout(stockRefreshRef.current);
        if (!activeBranchId || cart.state.items.length === 0) {
            setStockQuantities({});
            cart.setStockQuantities({}, {});
            return;
        }
        stockRefreshRef.current = setTimeout(() => {
            refreshStock(activeBranchId, cart.state.items);
        }, 100);
        return () => {
            if (stockRefreshRef.current) clearTimeout(stockRefreshRef.current);
        };
    }, [activeBranchId, cart.state.items, refreshStock]);

    // ── Contracts ──────────────────────────────────────────────────────────────
    const [contracts, setContracts] = useState<AvailableContract[]>([]);
    const [contractsLoading, setContractsLoading] = useState(true);

    const loadContracts = useCallback(async () => {
        if (!activeBranchId) return;
        setContractsLoading(true);
        const loadLocalContracts = () =>
            localRead.getAvailableContractsForPos(
                activeBranchId,
                user?.organization_id,
            );
        try {
            if (isOffline || !navigator.onLine || isBackendKnownUnreachable()) {
                setContracts(await loadLocalContracts());
                return;
            }

            const data = await contractsApi.getAvailableForPos(activeBranchId);
            if (data.length > 0) {
                setContracts(data);
                return;
            }

            const fallback = await loadLocalContracts();
            setContracts(fallback.length > 0 ? fallback : data);
        } catch {
            // Non-blocking — cashier can still proceed with empty list
            setContracts(await loadLocalContracts());
        } finally {
            setContractsLoading(false);
        }
    }, [activeBranchId, isOffline, user?.organization_id]);

    useEffect(() => {
        loadContracts();
    }, [loadContracts]);

    // ── Checkout ───────────────────────────────────────────────────────────────
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [checkoutError, setCheckoutError] = useState<string | null>(null);
    const [successResult, setSuccessResult] = useState<ProcessSaleResponse | null>(null);
    const checkoutInFlightRef = useRef(false);
    const checkoutIntentRef = useRef<CheckoutIntent | null>(null);

    useEffect(() => {
        checkoutIntentRef.current = null;
    }, [activeBranchId]);

    const handleCheckout = useCallback(async () => {
        if (checkoutInFlightRef.current) return;
        if (!activeBranchId) {
            setCheckoutError("Select an active branch before completing a sale.");
            return;
        }

        if (!cart.isValid) {
            setCheckoutError(cart.validationErrors[0]?.message ?? "Complete the required checkout fields.");
            return;
        }

        // Lock immediately — before any await — so rapid double-clicks are
        // blocked even while the async stock validation is in progress.
        checkoutInFlightRef.current = true;
        setIsSubmitting(true);
        setCheckoutError(null);

        // Pre-checkout stock validation — re-query for latest accuracy
        try {
            for (const item of cart.state.items) {
                const info = await localRead.getSellableQuantity(activeBranchId, item.drug.id);
                if (info.notStocked) {
                    setCheckoutError(`${item.drug.name} is not stocked at the active branch and cannot be sold.`);
                    checkoutInFlightRef.current = false;
                    setIsSubmitting(false);
                    return;
                }
                if (info.sellable <= 0) {
                    setCheckoutError(`${item.drug.name} has no sellable stock at this branch. Cannot complete sale.`);
                    checkoutInFlightRef.current = false;
                    setIsSubmitting(false);
                    return;
                }
                if (item.quantity > info.sellable) {
                    setCheckoutError(`Insufficient stock for ${item.drug.name}. Requested ${item.quantity}, but only ${info.sellable} available.`);
                    checkoutInFlightRef.current = false;
                    setIsSubmitting(false);
                    return;
                }
            }
        } catch (err) {
            setCheckoutError("Unable to verify stock availability. Ensure you are online or synchronize inventory first.");
            checkoutInFlightRef.current = false;
            setIsSubmitting(false);
            return;
        }

        try {
            const intent = checkoutIntentRef.current ?? (() => {
                const saleId = crypto.randomUUID();
                const branchFragment = activeBranchId.replace(/-/g, "").slice(0, 8);
                const randomFragment = saleId.replace(/-/g, "").slice(0, 8);
                return {
                    saleId,
                    saleNumber: `OFFLINE-${branchFragment}-${Date.now().toString(36)}-${randomFragment}`,
                    idempotencyKey: `sale:${saleId}`,
                };
            })();
            checkoutIntentRef.current = intent;

            let payload = {
                ...cart.buildSaleCreate(activeBranchId),
                client_sale_id: intent.saleId,
            };
            const selectedContract = cart.state.contract;
            const requiresOnlineContractVerification = Boolean(
                selectedContract &&
                (
                    selectedContract.requires_verification ||
                    selectedContract.requires_approval ||
                    selectedContract.type === "insurance"
                )
            );
        
            // ─── Pre-flight check: Validate prescription refills if used ───────────────
            let selectedRxNumber: string | null = null;
            let selectedRxPrescriberName: string | null = null;
            let selectedRxPrescriberLicense: string | null = null;

            if (payload.prescription_id) {
                const db = await (await import("@/lib/localDb")).getDb();
                const presResult = await db.select<Array<{ medications: string; refills_remaining: number; status: string; prescription_number: string; prescriber_name: string; prescriber_license: string }>>(
                    `SELECT medications, refills_remaining, status, prescription_number, prescriber_name, prescriber_license FROM prescriptions WHERE id = $1`,
                    [payload.prescription_id]
                );
            
                if (!presResult.length) {
                    setCheckoutError("Prescription not found. Please select a valid prescription.");
                    return;
                }
            
                const { medications, refills_remaining, status, prescription_number, prescriber_name, prescriber_license } = presResult[0];
                if (status !== "active") {
                    setCheckoutError(`Prescription is ${status} — cannot be used for this sale.`);
                    return;
                }
            
                if (refills_remaining <= 0) {
                    setCheckoutError("No refills remaining on this prescription. Please use a different prescription.");
                    return;
                }

                // Verify that the prescription contains the drug(s) being purchased
                let parsedMedications: Array<{ drug_id: string }> = [];
                try {
                    parsedMedications = typeof medications === "string" ? JSON.parse(medications) : medications;
                } catch {
                    parsedMedications = [];
                }

                const rxDrugIds = new Set(parsedMedications.map(m => m.drug_id));
                const cartRxItems = cart.state.items.filter(item => item.requiresPrescription);
                const uncoveredItems = cartRxItems.filter(item => !rxDrugIds.has(item.drug.id));

                if (uncoveredItems.length > 0) {
                    setCheckoutError(
                        "Selected prescription is not valid for all prescription-required drugs in the cart. Missing: " +
                        uncoveredItems.map(i => i.drug.name).join(", ")
                    );
                    return;
                }

                selectedRxNumber = prescription_number;
                selectedRxPrescriberName = prescriber_name;
                selectedRxPrescriberLicense = prescriber_license;
            }
        
            const backendWasReachable = isBackendReachable();
            const recordOfflineSale = async (): Promise<ProcessSaleResponse> => {
            const saleId = intent.saleId;
            const saleNumber = intent.saleNumber;
            const now = new Date().toISOString();
            const totals = cart.totals;
            
            // Defensive guards: ensure all numeric values are valid numbers
            const sanitizeNum = (val: unknown, fallback = 0): number => {
                const num = typeof val === 'number' ? val : Number(val);
                return !isNaN(num) ? num : fallback;
            };

            const offlineSale = {
                id: saleId,
                sale_number: saleNumber,
                organization_id: user?.organization_id ?? "",
                branch_id: activeBranchId,
                customer_id: payload.customer_id ?? null,
                customer_name: (payload.customer_id ? cart.state.customerName : payload.customer_name) || null,
                subtotal: sanitizeNum(totals.subtotal),
                discount_amount: sanitizeNum(totals.discountAmount),
                contract_discount_amount: sanitizeNum(totals.discountAmount),
                additional_discount_amount: 0,
                total_discount_amount: sanitizeNum(totals.discountAmount),
                tax_amount: sanitizeNum(totals.taxAmount),
                total_amount: sanitizeNum(totals.total),
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
                prescription_number: selectedRxNumber,
                prescriber_name: selectedRxPrescriberName,
                prescriber_license: selectedRxPrescriberLicense,
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
                refunded_by: null,
                refund_reason: null,
                refund_reference: null,
                receipt_printed: false,
                receipt_emailed: false,
                sync_status: "pending" as const,
                sync_version: 1,
                synced_at: null,
                updated_at: now,
                created_at: now,
                items: cart.state.items.map((item) => {
                    const unitPrice = Number(item.drug.unit_price) || 0;
                    const qty = Number(item.quantity) || 0;
                    const lineSubtotal = unitPrice * qty;
                    const discountPct = Number(cart.state.contract?.discount_percentage) || 0;
                    const contractDiscountAmt = parseFloat(((lineSubtotal * discountPct) / 100).toFixed(2)) || 0;
                    const taxRate = Number(item.drug.tax_rate) || 0;
                    const taxAmt = taxInclusive
                        ? parseFloat(((lineSubtotal * taxRate) / (100 + taxRate)).toFixed(2)) || 0
                        : parseFloat(((lineSubtotal * taxRate) / 100).toFixed(2)) || 0;
                    return {
                        id: crypto.randomUUID(),
                        sale_id: saleId,
                        drug_id: item.drug.id,
                        drug_name: item.drug.name,
                        drug_sku: item.drug.sku,
                        drug_generic_name: item.drug.generic_name,
                        usage_instructions: item.drug.usage_instructions,
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
                        total_price: taxInclusive
                            ? lineSubtotal - contractDiscountAmt
                            : lineSubtotal - contractDiscountAmt + taxAmt,
                        applied_contract_id: payload.price_contract_id,
                        applied_contract_name: cart.state.contract?.name ?? null,
                        insurance_covered: false,
                        patient_copay: null,
                        requires_prescription: item.requiresPrescription,
                        prescription_verified: item.prescriptionVerified,
                        prescription_id: item.prescriptionVerified ? (payload.prescription_id ?? null) : null,
                        allergy_check_performed: false,
                        created_at: now,
                        updated_at: now,
                    };
                }),
            };

            // Use robust transactional offline sales manager
            const inventoryDeltas = cart.state.items.map((item) => ({
                drug_id: item.drug.id,
                delta: -item.quantity,
            }));
            const recordResult = await offlineSalesManager.recordSaleTransaction(
                offlineSale as unknown as Parameters<typeof offlineSalesManager.recordSaleTransaction>[0],
                offlineSale.items,
                inventoryDeltas,
                intent.idempotencyKey
            );

            if (!recordResult.success) {
                throw new Error(`Failed to record offline sale: ${recordResult.error}`);
            }

            return {
                sale: {
                    ...offlineSale,
                    customer_full_name: offlineSale.customer_name,
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
                    items_count: offlineSale.items.length,
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
                message: "Sale recorded offline - will sync when connection is restored",
                warnings: ["Sale recorded offline and will be synced when back online"],
            };
            };

            try {
            let result: ProcessSaleResponse;

            if (shouldUseOfflineSalePath({
                browserOnline: navigator.onLine,
                appOffline: isOffline,
                backendReachable: backendWasReachable,
            })) {
                // ── Offline path ─────────────────────────────────────────────
                if (requiresOnlineContractVerification) {
                    throw new Error("This contract requires online verification before checkout.");
                }
                result = await recordOfflineSale();
            } else {
                // ── Online path ──────────────────────────────────────────────
                const eligibility = await contractsApi.verifyEligibility({
                    contract_id: payload.price_contract_id,
                    customer_id: payload.customer_id ?? null,
                    drug_ids: payload.items.map((item) => item.drug_id),
                    branch_id: activeBranchId,
                    sale_amount: cart.totals.total,
                });
                if (!eligibility.eligible) {
                    throw new Error(eligibility.message);
                }
                if (eligibility.verification_token) {
                    payload = {
                        ...payload,
                        contract_verification_token: eligibility.verification_token,
                    };
                }
                result = await salesApi.processSale(payload);
            }

            setSuccessResult(result);
            // Notify inventory page to refresh its counts
            appEvents.emit("inventory:changed");
            appEvents.emit("sales:changed");
            } catch (err) {
            const shouldRecordOffline = shouldFallbackToOfflineSaleAfterError({
                offlineError: isOfflineError(err),
                browserOnline: navigator.onLine,
                appOffline: isOffline,
                backendReachable: isBackendReachable(),
                backendReachableBeforeAttempt: backendWasReachable,
            });

            if (shouldRecordOffline) {
                if (requiresOnlineContractVerification) {
                    setCheckoutError("This contract requires online verification before checkout.");
                    return;
                }
                try {
                    const result = await recordOfflineSale();
                    setSuccessResult(result);
                    appEvents.emit("inventory:changed");
                    appEvents.emit("sales:changed");
                } catch (offlineError) {
                    setCheckoutError(parseApiError(offlineError));
                }
            } else {
                setCheckoutError(parseApiError(err));
            }
            }
        } catch (error) {
            setCheckoutError(parseApiError(error));
        } finally {
            checkoutInFlightRef.current = false;
            setIsSubmitting(false);
        }
    }, [cart, activeBranchId, isOffline, user]);

    const handleNewSale = useCallback(() => {
        setSuccessResult(null);
        setCheckoutError(null);
        checkoutIntentRef.current = null;
        cart.clearCart();
    }, [cart]);

    // Clear cart when branch changes
    const prevBranchRef = useRef(activeBranchId);
    useEffect(() => {
        if (prevBranchRef.current && prevBranchRef.current !== activeBranchId) {
            cart.clearCart();
            setCheckoutError("Branch changed — cart cleared. Please review stock at the new branch.");
            checkoutIntentRef.current = null;
        }
        prevBranchRef.current = activeBranchId;
    }, [activeBranchId, cart]);

    const handleClearCart = useCallback(() => {
        checkoutIntentRef.current = null;
        setCheckoutError(null);
        cart.clearCart();
    }, [cart]);

    // Drug IDs already in cart — used to show "In cart" state in search panel
    const cartDrugIds = new Set(cart.state.items.map((i) => i.drug.id));

    const fmtGHS = (n: number) => `₵${n.toFixed(2)}`;

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between flex-shrink-0">
                <div>
                    <h1 className="font-display text-2xl font-bold text-ink">Point of Sale</h1>
                    <p className="text-sm text-ink-muted mt-0.5">
                        {user?.full_name} · {user?.is_super_admin ? "Super Admin" : user?.roles?.map(r => r.name).join(", ")}
                    </p>
                </div>

                <div className="flex items-center gap-4">
                    <div className="bg-brand-50 border border-brand-100 px-4 py-2 rounded-2xl text-right">
                        <p className="text-[10px] font-bold text-brand-600 uppercase tracking-widest">Today's Sales</p>
                        <p className="text-2xl font-black text-brand-700">{fmtGHS(todaySales)}</p>
                    </div>
                </div>
            </div>

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
                        checkoutError={checkoutError}
                        isSubmitting={isSubmitting}
                        taxInclusive={taxInclusive}
                        stockQuantities={stockQuantities}
                        onSetQuantity={cart.setQuantity}
                        onRemoveItem={cart.removeItem}
                        onSetPrescriptionVerified={cart.setPrescriptionVerified}
                        onSetContract={cart.setContract}
                        onSetCustomerId={cart.setCustomerId}
                        onSetCustomerName={cart.setCustomerName}
                        onSetPaymentMethod={cart.setPaymentMethod}
                        onSetAmountPaid={cart.setAmountPaid}
                        onSetSplitPayment={cart.setSplitPayment}
                        onSetPrescriptionId={cart.setPrescriptionId}
                        onSetInsuranceClaimNumber={cart.setInsuranceClaimNumber}
                        onSetInsurancePreAuthNumber={cart.setInsurancePreAuthNumber}
                        onSetInsuranceVerified={cart.setInsuranceVerified}
                        onSetNotes={cart.setNotes}
                        onCheckout={handleCheckout}
                        onClearCart={handleClearCart}
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
                        footerMessage={footerMessage}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}
