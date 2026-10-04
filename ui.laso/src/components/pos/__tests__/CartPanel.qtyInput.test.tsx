/**
 * @vitest-environment jsdom
 *
 * The quantity input's editing behaviour.
 *
 * The two bugs it fixes, both observed at the till:
 *   - focusing did not select the number, so typing "133" into "1" gave 1133,
 *     which the reducer then capped to 1000 — a silent wrong quantity;
 *   - the field could not be cleared, because `parseInt(v) || 1` turned "" into 1
 *     the instant you deleted it.
 *
 * useCart's validation is NOT touched. It remains the safety net for the case
 * where stock refreshes below the quantity AFTER the cashier committed.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import {
    CartPanel,
    commitDraft,
    sanitiseDraft,
} from "@/components/pos/CartPanel";
import type { CartItem } from "@/hooks/useCart";
import type { Drug } from "@/types";

function drug(over: Partial<Drug> = {}): Drug {
    return {
        id: "d1",
        name: "Gebedol",
        unit_price: 5,
        reorder_level: 300,
        reorder_quantity: 300,
        ...over,
    } as unknown as Drug;
}

function item(over: Partial<CartItem> = {}): CartItem {
    return {
        drug: drug(),
        quantity: 1,
        requiresPrescription: false,
        prescriptionVerified: false,
        batchId: null,
        ...over,
    } as CartItem;
}

/**
 * Renders the panel and keeps the cart in sync the way the real page does, so
 * a committed quantity flows back into the field.
 */
function setup(over: { quantity?: number; available?: number } = {}) {
    const state = { quantity: over.quantity ?? 1 };
    const onSetQuantity = vi.fn((_id: string, q: number) => {
        state.quantity = q;
        rerender();
    });
    const view = render(
        <CartPanel
            items={[item({ quantity: state.quantity })]}
            contract={null}
            contracts={[]}
            contractsLoading={false}
            customerName=""
            customerId={null}
            paymentMethod="cash"
            amountPaid={0}
            prescriptionId={null}
            insuranceClaimNumber=""
            insurancePreAuthNumber=""
            insuranceVerified={false}
            notes=""
            totals={{ subtotal: 0, tax: 0, total: 0, discount: 0 } as never}
            validationErrors={[]}
            checkoutError={null}
            isSubmitting={false}
            stockQuantities={over.available === undefined ? {} : { d1: over.available }}
            onSetQuantity={onSetQuantity}
            onRemoveItem={() => {}}
            onSetPrescriptionVerified={() => {}}
            onSetContract={() => {}}
            onSetCustomerId={() => {}}
            onSetCustomerName={() => {}}
            onSetPaymentMethod={() => {}}
            onSetAmountPaid={() => {}}
            onSetSplitPayment={() => {}}
            onSetPrescriptionId={() => {}}
            onSetInsuranceClaimNumber={() => {}}
            onSetInsurancePreAuthNumber={() => {}}
            onSetInsuranceVerified={() => {}}
            onSetNotes={() => {}}
            onCheckout={() => {}}
            onClearCart={() => {}}
        />
    );

    function rerender() {
        view.rerender(
            <CartPanel
                items={[item({ quantity: state.quantity })]}
                contract={null}
                contracts={[]}
                contractsLoading={false}
                customerName=""
                customerId={null}
                paymentMethod="cash"
                amountPaid={0}
                prescriptionId={null}
                insuranceClaimNumber=""
                insurancePreAuthNumber=""
                insuranceVerified={false}
                notes=""
                totals={{ subtotal: 0, tax: 0, total: 0, discount: 0 } as never}
                validationErrors={[]}
                checkoutError={null}
                isSubmitting={false}
                stockQuantities={over.available === undefined ? {} : { d1: over.available }}
                onSetQuantity={onSetQuantity}
                onRemoveItem={() => {}}
                onSetPrescriptionVerified={() => {}}
                onSetContract={() => {}}
                onSetCustomerId={() => {}}
                onSetCustomerName={() => {}}
                onSetPaymentMethod={() => {}}
                onSetAmountPaid={() => {}}
                onSetSplitPayment={() => {}}
                onSetPrescriptionId={() => {}}
                onSetInsuranceClaimNumber={() => {}}
                onSetInsurancePreAuthNumber={() => {}}
                onSetInsuranceVerified={() => {}}
                onSetNotes={() => {}}
                onCheckout={() => {}}
                onClearCart={() => {}}
            />
        );
    }

    const input = () => screen.getByTestId("qty-input-d1") as HTMLInputElement;
    const label = () => screen.getByTestId("stock-label-d1");
    return { input, label, onSetQuantity, state, view };
}

describe("draft sanitising", () => {
    it("keeps digits only", () => {
        expect(sanitiseDraft("133")).toBe("133");
        expect(sanitiseDraft("1a3b3")).toBe("133");
        expect(sanitiseDraft("abc")).toBe("");
        expect(sanitiseDraft("1.5")).toBe("15");
        expect(sanitiseDraft("-5")).toBe("5");
    });

    it("strips leading zeros but keeps a lone zero as 0", () => {
        expect(sanitiseDraft("007")).toBe("7");
        expect(sanitiseDraft("0")).toBe("0");
        expect(sanitiseDraft("00")).toBe("0");
    });

    it("allows empty so the field can be cleared", () => {
        expect(sanitiseDraft("")).toBe("");
    });
});

describe("committing a draft", () => {
    it("empty / NaN / < 1 becomes 1", () => {
        expect(commitDraft("", 113)).toBe(1);
        expect(commitDraft("abc", 113)).toBe(1);
        expect(commitDraft("0", 113)).toBe(1);
    });

    it("clamps to available when it is known", () => {
        expect(commitDraft("133", 113)).toBe(113);
        expect(commitDraft("50", 113)).toBe(50);
    });

    it("falls back to the 1000 cap when available is unknown", () => {
        expect(commitDraft("133", undefined)).toBe(133);
        expect(commitDraft("5000", undefined)).toBe(1000);
    });
});

describe("editing the quantity field", () => {
    it("selects the whole number on focus, so typing replaces it", () => {
        const { input } = setup({ quantity: 1, available: 200 });
        const el = input();
        const select = vi.spyOn(el, "select");
        fireEvent.focus(el);
        expect(select).toHaveBeenCalled();
    });

    it("typing 133 with stock >= 133 commits 133", () => {
        const { input, onSetQuantity } = setup({ quantity: 1, available: 200 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "133" } });
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 133);
    });

    it("typing 133 with stock 113 clamps to 113 and shows the max label", () => {
        const { input, onSetQuantity, label } = setup({ quantity: 1, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "133" } });
        fireEvent.blur(el);
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 113);
        expect(label().textContent).toContain("Max reached (113)");
    });

    it("an empty draft then blur commits 1", () => {
        const { input, onSetQuantity } = setup({ quantity: 5, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "" } });
        // Not committed while empty.
        expect(onSetQuantity).not.toHaveBeenLastCalledWith("d1", 1);
        fireEvent.blur(el);
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 1);
    });

    it("'0' becomes 1 on blur", () => {
        const { input, onSetQuantity } = setup({ quantity: 5, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "0" } });
        fireEvent.blur(el);
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 1);
    });

    it("letters are ignored rather than becoming 1", () => {
        const { input, onSetQuantity } = setup({ quantity: 5, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "abc" } });
        fireEvent.blur(el);
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 1);
    });

    it("Escape reverts the draft to the committed value", () => {
        // 99 is within stock so it commits as typed (spec d). 200 is over the
        // limit so it stays a draft. Escape must abandon the 200 and fall back
        // to the committed 99 — NOT commit 200 on the blur that follows.
        const { input, onSetQuantity, state } = setup({ quantity: 5, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "99" } });
        expect(state.quantity).toBe(99);
        fireEvent.change(el, { target: { value: "200" } });
        expect(onSetQuantity).not.toHaveBeenLastCalledWith("d1", 200);
        fireEvent.keyDown(el, { key: "Escape" });
        expect(onSetQuantity).not.toHaveBeenLastCalledWith("d1", 200);
        expect(state.quantity).toBe(99);
        expect(input().value).toBe("99");
    });

    it("Enter commits the draft", () => {
        const { input, onSetQuantity } = setup({ quantity: 1, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "7" } });
        fireEvent.keyDown(el, { key: "Enter" });
        expect(onSetQuantity).toHaveBeenLastCalledWith("d1", 7);
    });

    it("the 1000 cap still applies when stock is unknown", () => {
        const { input, onSetQuantity } = setup({ quantity: 1, available: undefined });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "5000" } });
        fireEvent.blur(el);
        // commitDraft caps at 1000; the reducer caps again, so the committed
        // value never exceeds the documented ceiling.
        expect(onSetQuantity.mock.calls.every(([, q]) => q <= 1000)).toBe(true);
    });

    it("+/- keep the field in step with the cart", () => {
        const { input, state } = setup({ quantity: 5, available: 113 });
        const plus = screen.getByLabelText("Increase quantity");
        fireEvent.click(plus);
        expect(state.quantity).toBe(6);
        fireEvent.click(screen.getByLabelText("Decrease quantity for Gebedol"));
        expect(state.quantity).toBe(5);
        // And the field shows the committed number, not a stale draft.
        expect(input().value).toBe("5");
    });

    it("a stale draft cannot survive a button press", () => {
        // "200" is over the limit so it stays an uncommitted draft. Clicking "+"
        // moves the cart to 6; the blur that follows must not commit 200.
        const { input, onSetQuantity, state } = setup({ quantity: 5, available: 113 });
        const el = input();
        fireEvent.focus(el);
        fireEvent.change(el, { target: { value: "200" } });
        fireEvent.click(screen.getByLabelText("Increase quantity"));
        expect(state.quantity).toBe(6);
        fireEvent.blur(el);
        expect(onSetQuantity).not.toHaveBeenLastCalledWith("d1", 200);
        expect(state.quantity).toBe(6);
    });

    it("shows the shortfall band when stock drops below the committed quantity", () => {
        // useCart's validation is the safety net; it is not modified. This pins
        // that the band still appears once stock refreshes below the line.
        const { view } = setup({ quantity: 20, available: 10 });
        expect(screen.getByText("Only 10 available (requested 20)")).toBeTruthy();
        view.unmount();
    });
});
