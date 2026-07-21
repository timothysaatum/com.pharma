/** @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AvailableContract } from "@/api/contracts";
import { useCart } from "@/hooks/useCart";
import type { Drug } from "@/types";

const corporateContract: AvailableContract = {
    id: "contract-corporate",
    code: "CORP-001",
    name: "Corporate",
    type: "corporate",
    discount_percentage: 10,
    is_default: false,
    requires_verification: false,
    requires_approval: false,
    display: "Corporate",
    warning: null,
    copay_amount: null,
    copay_percentage: null,
    requires_preauthorization: false,
    insurance_provider_id: null,
    daily_usage_limit: null,
    per_customer_usage_limit: null,
    applies_to_prescription_only: false,
    applies_to_otc: true,
    minimum_purchase_amount: null,
    maximum_purchase_amount: null,
};

const prescriptionDrug = {
    id: "drug-rx",
    name: "Prescription Drug",
    unit_price: 100,
    tax_rate: 0,
    requires_prescription: true,
} as Drug;

describe("useCart corporate checkout", () => {
    it("uses credit for a corporate Rx sale and charges the discounted total", () => {
        const { result } = renderHook(() => useCart());

        act(() => {
            result.current.addItem(prescriptionDrug);
            result.current.setStockQuantities({ [prescriptionDrug.id]: 10 }, {});
            result.current.setContract(corporateContract);
            result.current.setCustomerId("customer-corporate");
            result.current.setPrescriptionId("prescription-1");
            result.current.setPrescriptionVerified(prescriptionDrug.id, true);
        });

        expect(result.current.state.paymentMethod).toBe("credit");
        expect(result.current.totals.total).toBe(90);
        expect(result.current.totals.patientCopay).toBe(0);
        expect(result.current.totals.amountDue).toBe(90);
        expect(result.current.validationErrors).toEqual([]);

        const payload = result.current.buildSaleCreate("branch-1");
        expect(payload).toMatchObject({
            price_contract_id: corporateContract.id,
            customer_id: "customer-corporate",
            prescription_id: "prescription-1",
            payment_method: "credit",
            insurance_verified: false,
        });
        expect(payload.amount_paid).toBeUndefined();
        expect(payload.insurance_claim_number).toBeUndefined();
    });
});
