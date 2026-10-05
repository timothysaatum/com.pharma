/**
 * @vitest-environment jsdom
 *
 * CartPanel's stock label and "+" button, rendered for real.
 *
 * Existing POS tests mock CartPanel out entirely, so nothing previously covered
 * what the cashier actually reads on a cart line.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CartPanel } from "@/components/pos/CartPanel";
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

function renderPanel(over: {
    items?: CartItem[];
    stockQuantities?: Record<string, number>;
    onSetQuantity?: (id: string, q: number) => void;
} = {}) {
    const noop = () => {};
    return render(
        <CartPanel
            items={over.items ?? [item()]}
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
            stockQuantities={over.stockQuantities ?? {}}
            onSetQuantity={over.onSetQuantity ?? noop}
            onRemoveItem={noop}
            onSetPrescriptionVerified={noop}
            onSetContract={noop}
            onSetCustomerId={noop}
            onSetCustomerName={noop}
            onSetPaymentMethod={noop}
            onSetAmountPaid={noop}
            onSetSplitPayment={noop}
            onSetPrescriptionId={noop}
            onSetInsuranceClaimNumber={noop}
            onSetInsurancePreAuthNumber={noop}
            onSetInsuranceVerified={noop}
            onSetNotes={noop}
            onCheckout={noop}
            onClearCart={noop}
        />
    );
}

function line(): HTMLElement {
    return screen.getByTestId("stock-label-d1");
}
function plusButton(): HTMLElement {
    return screen.getByLabelText(/Increase quantity/i);
}

describe("cart line stock label", () => {
    it("ok: shows the available count, muted", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: { d1: 113 } });
        expect(line().textContent).toContain("113 available");
        expect(line().className).toContain("muted");
        expect(line().getAttribute("data-state")).toBe("ok");
    });

    it("unknown: says it is checking, and does not imply zero", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: {} });
        expect(line().textContent).toContain("Checking stock…");
        expect(line().textContent ?? "").not.toContain("0");
    });

    it("max: says the limit was reached, amber", () => {
        renderPanel({ items: [item({ quantity: 113 })], stockQuantities: { d1: 113 } });
        expect(line().textContent).toContain("Max reached (113)");
        expect(line().className).toContain("amber");
    });

    it("low: says only N left, amber", () => {
        renderPanel({ items: [item({ quantity: 1 })], stockQuantities: { d1: 3 } });
        expect(line().textContent).toContain("Only 3 left");
        expect(line().className).toContain("amber");
    });

    it("over: shows the available count in red", () => {
        renderPanel({ items: [item({ quantity: 20 })], stockQuantities: { d1: 10 } });
        expect(line().textContent).toContain("10 available");
        expect(line().className).toContain("red");
    });

    it("is announced politely and does not wrap", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: { d1: 113 } });
        expect(line().getAttribute("aria-live")).toBe("polite");
        expect(line().className).toContain("whitespace-nowrap");
        expect(line().className).toContain("truncate");
        expect(line().className).toContain("tabular-nums");
    });

    it("keeps the existing red shortfall band unchanged when over", () => {
        renderPanel({ items: [item({ quantity: 20 })], stockQuantities: { d1: 10 } });
        // Exact wording is part of the contract with the cashier.
        expect(screen.getByText("Only 10 available (requested 20)")).toBeTruthy();
    });

    it("does not show the shortfall band when within stock", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: { d1: 113 } });
        expect(screen.queryByText(/Only 113 available \(requested/)).toBeNull();
    });

    it("falls back through available_quantity -> valid_batch_quantity -> quantity", () => {
        renderPanel({
            items: [item({ drug: drug({ available_quantity: 113 } as Partial<Drug>) })],
            stockQuantities: {},
        });
        expect(line().textContent).toContain("113 available");

        renderPanel({
            items: [
                item({
                    drug: drug({
                        valid_batch_quantity: 77,
                        quantity: 40,
                    } as Partial<Drug>),
                }),
            ],
            stockQuantities: {},
        });
        expect(screen.getAllByTestId("stock-label-d1")[1].textContent).toContain("77 available");
    });
});

describe("the '+' button at the stock limit", () => {
    it("is disabled with a tooltip and an aria-label naming the limit", () => {
        renderPanel({ items: [item({ quantity: 113 })], stockQuantities: { d1: 113 } });
        const btn = plusButton() as HTMLButtonElement;
        expect(btn.disabled).toBe(true);
        expect(btn.getAttribute("title")).toBe("Only 113 available");
        expect(btn.getAttribute("aria-label")).toContain("only 113 available");
    });

    it("stays enabled below the limit", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: { d1: 113 } });
        const btn = plusButton() as HTMLButtonElement;
        expect(btn.disabled).toBe(false);
        expect(btn.getAttribute("title")).toBeNull();
    });

    it("stays enabled when stock is unknown", () => {
        renderPanel({ items: [item({ quantity: 5 })], stockQuantities: {} });
        expect((plusButton() as HTMLButtonElement).disabled).toBe(false);
    });

    it("still increments while enabled", () => {
        const onSetQuantity = vi.fn();
        renderPanel({
            items: [item({ quantity: 5 })],
            stockQuantities: { d1: 113 },
            onSetQuantity,
        });
        fireEvent.click(plusButton());
        expect(onSetQuantity).toHaveBeenCalledWith("d1", 6);
    });

    it("keeps the 1000 fallback cap when stock is unknown", () => {
        const onSetQuantity = vi.fn();
        renderPanel({ items: [item({ quantity: 999 })], stockQuantities: {}, onSetQuantity });
        fireEvent.click(plusButton());
        expect(onSetQuantity).toHaveBeenCalledWith("d1", 1000);
    });

    it("does not call through when disabled at the limit", () => {
        const onSetQuantity = vi.fn();
        renderPanel({
            items: [item({ quantity: 113 })],
            stockQuantities: { d1: 113 },
            onSetQuantity,
        });
        fireEvent.click(plusButton());
        expect(onSetQuantity).not.toHaveBeenCalled();
    });
});
