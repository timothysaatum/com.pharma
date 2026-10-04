/**
 * @vitest-environment jsdom
 *
 * THE OBSERVED BUG (screenshots 2026-10-04)
 * ----------------------------------------
 * The picker showed "— Select contract —" with the red "Select a price contract"
 * messages and a disabled sale button, even though the list had loaded and held
 * "STANDARD PRICE (Standard)". It happened after sales were completed in the same
 * session; earlier in that session the same list auto-selected.
 *
 * Cause: CartPanel latched its auto-select behind `autoSelectedRef`, set true on
 * the first selection and never reset anywhere. `contract` was in the effect's
 * deps, so the effect DID re-run after `clearCart()` nulled the selection — but
 * the latch short-circuited it, so nothing was re-selected for the rest of the
 * session. The latch is also why it looked intermittent: it worked exactly once
 * per mounted POSPage.
 *
 * A real useCart drives these tests, not a mock. The bug lives in the
 * interaction between the reducer's reset and the panel's effect, so a mocked
 * onSetContract cannot reproduce it. The hook and the panel share ONE component
 * tree, so a dispatch re-renders the panel with fresh state exactly as it does
 * in the app.
 */
import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { CartPanel } from "@/components/pos/CartPanel";
import { useCart } from "@/hooks/useCart";
import type { AvailableContract } from "@/api/contracts";
import type { Drug } from "@/types";

vi.mock("@/components/pos/PrescriptionSelector", () => ({
    PrescriptionSelector: () => null,
}));

function contract(
    id: string,
    name: string,
    type: AvailableContract["type"],
    isDefault: boolean
): AvailableContract {
    return {
        id,
        code: id.toUpperCase(),
        name,
        type,
        is_default: isDefault,
        discount_percentage: 0,
        requires_verification: false,
        requires_approval: false,
        display: name,
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
    } as unknown as AvailableContract;
}

const STANDARD = contract("std", "STANDARD PRICE", "standard", true);
const CORPORATE = contract("corp", "Corporate Contract", "corporate", false);
const INSURANCE = contract("ins", "GLICO Health", "insurance", false);

const DRUG = {
    id: "d1",
    name: "Gebedol",
    unit_price: 5,
    reorder_level: 300,
    reorder_quantity: 300,
} as unknown as Drug;

type CartApi = ReturnType<typeof useCart>;
type Issue = {
    kind: "offline" | "error" | "empty" | null;
    message: string | null;
} | null;

/**
 * Mounts CartPanel wired to a real useCart in the same tree, and exposes the
 * latest cart state for assertions.
 *
 * NOTE ON VISIBILITY: CartPanel renders "Cart is empty" INSTEAD of the whole
 * checkout form when there are no items (CartPanel.tsx:578), so the contract
 * picker does not exist in the DOM for an empty cart. Tests therefore assert on
 * the cart STATE after a reset — which is the real invariant, and the one the
 * effect guarantees — and then add an item to assert what the cashier SEES.
 * That also mirrors the manual check: after Clear you re-add a drug to see the
 * picker again.
 */
function setup(opts: {
    contracts: AvailableContract[];
    contractsIssue?: Issue;
    /** Put a drug in the cart so the checkout form (and picker) renders. */
    withItem?: boolean;
}) {
    const box: { cart: CartApi | null } = { cart: null };

    function Harness({
        list,
        issue,
    }: {
        list: AvailableContract[];
        issue: Issue;
    }) {
        const cart = useCart();
        box.cart = cart;
        const s = cart.state;
        return (
            <CartPanel
                items={s.items}
                contract={s.contract}
                contracts={list}
                contractsLoading={false}
                contractsIssue={issue}
                onRetryContracts={() => {}}
                customerName=""
                customerId={null}
                paymentMethod={s.paymentMethod}
                amountPaid={s.amountPaid}
                prescriptionId={null}
                insuranceClaimNumber=""
                insurancePreAuthNumber=""
                insuranceVerified={s.insuranceVerified}
                notes=""
                totals={{
                    subtotal: 5,
                    tax: 0,
                    total: 5,
                    discount: 0,
                    discountAmount: 0,
                    change: 0,
                    copay: 0,
                    patientCopay: 0,
                    insuranceShare: 0,
                    itemCount: s.items.length,
                } as never}
                validationErrors={[]}
                checkoutError={null}
                isSubmitting={false}
                stockQuantities={{ d1: 500 }}
                onSetQuantity={cart.setQuantity}
                onRemoveItem={cart.removeItem}
                onSetPrescriptionVerified={() => {}}
                onSetContract={cart.setContract}
                onSetCustomerId={() => {}}
                onSetCustomerName={() => {}}
                onSetPaymentMethod={cart.setPaymentMethod}
                onSetAmountPaid={cart.setAmountPaid}
                onSetSplitPayment={() => {}}
                onSetPrescriptionId={() => {}}
                onSetInsuranceClaimNumber={() => {}}
                onSetInsurancePreAuthNumber={() => {}}
                onSetInsuranceVerified={() => {}}
                onSetNotes={() => {}}
                onCheckout={() => {}}
                onClearCart={cart.clearCart}
            />
        );
    }

    const view = render(
        <Harness list={opts.contracts} issue={opts.contractsIssue ?? { kind: null, message: null }} />
    );

    const h = {
        view,
        get cart(): CartApi {
            if (!box.cart) throw new Error("cart not mounted");
            return box.cart;
        },
        get state() {
            return setupState(box);
        },
        /** Re-renders with a new contract list, as a refetch or branch change does. */
        setContracts(list: AvailableContract[], issue: Issue = { kind: null, message: null }) {
            view.rerender(<Harness list={list} issue={issue} />);
        },
        addItem() {
            act(() => {
                box.cart?.addItem(DRUG);
            });
        },
        clearCart() {
            act(() => {
                box.cart?.clearCart();
            });
        },
        choose(c: AvailableContract | null) {
            act(() => {
                box.cart?.setContract(c);
            });
        },
        /** Re-add a drug so the picker renders again after a reset. */
        readd() {
            act(() => {
                box.cart?.addItem(DRUG);
            });
        },
    };

    if (opts.withItem !== false) h.addItem();
    return h;
}

function setupState(box: { cart: CartApi | null }) {
    if (!box.cart) throw new Error("cart not mounted");
    return box.cart.state;
}

/**
 * The picker's selected value, read the way the DOM exposes it.
 *
 * Targeted by accessible name, not by position: the checkout form has a second
 * combobox (payment method), so "the first one" would be a silent trap.
 */
function selectedContract(): string {
    return contractSelect().value;
}

function contractSelect(): HTMLSelectElement {
    return screen.getByRole("combobox", { name: "Price contract" }) as HTMLSelectElement;
}

const PLACEHOLDER = "— Select contract —";

/** The default, as the cart holds it. Works even when the picker is unmounted. */
function expectCartHas(h: { state: { contract: AvailableContract | null } }, id: string) {
    expect(h.state.contract?.id ?? null).toBe(id);
}

describe("the POS always has a price contract selected", () => {
    it("selects the default on initial load", () => {
        setup({ contracts: [CORPORATE, STANDARD] });
        expect(selectedContract()).toBe("std");
    });

    it("REGRESSION: list loaded, selection cleared by a reset, default re-selected", () => {
        // The observed sequence, step for step.
        const h = setup({ contracts: [STANDARD, CORPORATE] });
        expect(selectedContract()).toBe("std");

        h.clearCart(); // POSPage handleNewSale / Clear / branch change

        // The panel put the default straight back. Asserted on the cart because
        // the picker is unmounted while the cart is empty.
        //
        // The selection is never observably null: CLEAR_CART and the effect's
        // re-selection both flush inside this act(), so there is no render in
        // between to catch. That is the desired outcome — with the old latch
        // this stayed null for the rest of the session.
        expectCartHas(h, "std");

        // And it is what the cashier sees once they start the next sale.
        h.readd();
        expect(selectedContract()).toBe("std");
    });

    it("re-selects after a completed sale and a New Sale", () => {
        const h = setup({ contracts: [STANDARD] });
        expect(selectedContract()).toBe("std");
        h.clearCart();
        expectCartHas(h, "std");
        h.readd();
        expect(selectedContract()).toBe("std");
    });

    it("re-selects after Clear with a quantity changed first", () => {
        // The manual check: add a drug, change the quantity, Clear, add again.
        const h = setup({ contracts: [STANDARD] });
        act(() => {
            h.cart.setQuantity("d1", 13);
        });
        expect(selectedContract()).toBe("std");
        h.clearCart();
        expectCartHas(h, "std");
        h.readd();
        expect(selectedContract()).toBe("std");
    });

    it("keeps re-selecting across repeated resets, not just the first", () => {
        // The latch version survived one reset only by luck. Over a shift this
        // is the difference between working and not.
        const h = setup({ contracts: [STANDARD] });
        for (let i = 0; i < 5; i++) {
            h.clearCart();
            expectCartHas(h, "std");
        }
    });

    it("does NOT override a contract the cashier chose for this cart", () => {
        const h = setup({ contracts: [STANDARD, CORPORATE] });
        h.choose(CORPORATE);
        expect(selectedContract()).toBe("corp");
        act(() => {
            h.cart.setQuantity("d1", 2); // re-render, no list change
        });
        expect(selectedContract()).toBe("corp");
    });

    it("returns to the default once the cart resets after a manual choice", () => {
        const h = setup({ contracts: [STANDARD, CORPORATE] });
        h.choose(CORPORATE);
        expect(selectedContract()).toBe("corp");
        h.clearCart();
        expectCartHas(h, "std");
        h.readd();
        expect(selectedContract()).toBe("std");
    });

    it("survives a list refresh that hands back new objects for the same ids", () => {
        // A refetch produces fresh objects. Comparing by identity would read that
        // as "the selection changed" and stomp the cashier's choice.
        const h = setup({ contracts: [STANDARD, CORPORATE] });
        h.choose(CORPORATE);
        h.setContracts([
            contract("std", "STANDARD PRICE", "standard", true),
            contract("corp", "Corporate Contract", "corporate", false),
        ]);
        expect(selectedContract()).toBe("corp");
    });

    it("falls back to the default when the selected contract vanishes from the list", () => {
        const h = setup({ contracts: [STANDARD, INSURANCE] });
        h.choose(INSURANCE);
        expect(selectedContract()).toBe("ins");

        // Refetch and the insurance contract is gone (lapsed, or no longer
        // valid at this branch). Nothing is validly selected any more.
        h.setContracts([STANDARD]);

        // Assert the CART, not only the DOM. The controlled <select> is given a
        // value that no longer exists among its options, and jsdom then falls
        // back to the first option — so the DOM read alone would report "std"
        // even while the cart still held the vanished contract, and this case
        // would pass without the fix doing anything.
        expectCartHas(h, "std");
        expect(selectedContract()).toBe("std");
    });

    it("selects the standard contract when nothing is flagged default", () => {
        const plainStd = contract("std2", "Standard Retail", "standard", false);
        const corp = contract("corp2", "Corporate", "corporate", false);
        setup({ contracts: [corp, plainStd] });
        expect(selectedContract()).toBe("std2");
    });

    it("shows the placeholder and selects nothing when the list is empty", () => {
        const h = setup({ contracts: [] });
        expect(h.state.contract).toBeNull();
        expect(screen.getByRole("option", { name: PLACEHOLDER })).toBeTruthy();
    });

    it("hides the placeholder when there is a contract to select", () => {
        // A placeholder here is a dead end: choosing it clears the selection
        // straight back into the validation error.
        setup({ contracts: [STANDARD, CORPORATE] });
        expect(screen.queryByRole("option", { name: PLACEHOLDER })).toBeNull();
    });

    it("selects nothing and keeps the error band when the load FAILED", () => {
        const h = setup({
            contracts: [],
            contractsIssue: {
                kind: "error",
                message: "Could not load price contracts from the server (network error).",
            },
        });
        expect(h.state.contract).toBeNull();
        expect(
            screen.getByText(/Could not load price contracts from the server/)
        ).toBeTruthy();
        expect(screen.getByRole("option", { name: PLACEHOLDER })).toBeTruthy();
    });

    it("selects the default after Retry succeeds", () => {
        const h = setup({
            contracts: [],
            contractsIssue: { kind: "error", message: "Could not load price contracts." },
        });
        expect(h.state.contract).toBeNull();

        // Retry succeeds, so the list arrives and the issue clears.
        h.setContracts([STANDARD], { kind: null, message: null });
        expect(selectedContract()).toBe("std");
        expect(screen.queryByText(/Could not load price contracts/)).toBeNull();
    });

    it("returns the payment method to cash when the default replaces an insurance contract", () => {
        const h = setup({ contracts: [STANDARD, INSURANCE] });
        h.choose(INSURANCE);
        expect(h.state.paymentMethod).toBe("insurance");

        h.clearCart(); // default (standard) replaces insurance
        expectCartHas(h, "std");
        expect(h.state.paymentMethod).toBe("cash");
    });

    it("clears the insurance verification state when the default replaces insurance", () => {
        const h = setup({ contracts: [STANDARD, INSURANCE] });
        h.choose(INSURANCE);
        h.clearCart();
        expect(h.state.insuranceVerified).toBe(false);
    });

    it("takes a corporate contract to credit, then back to cash on reset", () => {
        // SET_CONTRACT (useCart.ts:189-215) forces paymentMethod to "credit"
        // for a corporate contract and to "cash" when leaving insurance, so the
        // default replacing a corporate contract must not leave it on credit.
        const h = setup({ contracts: [STANDARD, CORPORATE] });
        h.choose(CORPORATE);
        expect(h.state.paymentMethod).toBe("credit");

        h.clearCart();
        expectCartHas(h, "std");
        expect(h.state.paymentMethod).toBe("cash");
    });

    it("does not loop when the default is the last option in the list", () => {
        // The old inline `?? contracts[0]` plus a latch could not loop; the new
        // rule must still settle when the chosen contract is last in the array.
        const h = setup({ contracts: [CORPORATE, INSURANCE, STANDARD] });
        expect(selectedContract()).toBe("std");
        act(() => {
            h.cart.setQuantity("d1", 2);
        });
        expect(selectedContract()).toBe("std");
    });
});
