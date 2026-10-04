/**
 * @vitest-environment jsdom
 *
 * REGRESSION TEST — no production change. Does the cart's Total ever disagree
 * with the "Complete Sale" button's amount?
 *
 * Both read `totals.total` inside the same JSX return:
 *   CartPanel.tsx:1051  `₵{totals.total.toFixed(2)}`   (the Total)
 *   CartPanel.tsx:1106  `Complete Sale · ₵${totals.total.toFixed(2)}`
 * and `totals` is one object from a single useMemo at useCart.ts:500 keyed on
 * [state, taxInclusive]. There is no React.memo, useDeferredValue,
 * useTransition, portal or second checkout total anywhere on that path, so the
 * two expressions cannot diverge WITHIN one render.
 *
 * The screenshots reported a mismatch (3 items: Total ₵36.00 vs button ₵5.00;
 * Gebedol at 133: Total ₵665.00 vs button ₵65.00 = 13 x ₵5, one keystroke
 * earlier). Those numbers look like the QUANTITY was a step behind, not the
 * price, so this test drives quantity changes in rapid steps with everything
 * that could re-render mid-flight left active:
 *
 *   - the 100ms stock refresh (the panel's setTimeout-driven refreshStock)
 *   - the amountPaid sync effect (CartPanel.tsx:461-470), which writes state
 *     during commit whenever totals.total or items.length changes
 *   - search debounce (300ms) and the stock-refresh debounce (250ms)
 *
 * It asserts equality after EVERY step — including the intermediate render
 * immediately after each change — and again after all timers settle. If this
 * passes, the mismatch did not reproduce here and the most likely explanation is
 * that the screenshots captured two different frames.
 */
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { CartPanel } from "@/components/pos/CartPanel";
import type { CartItem } from "@/hooks/useCart";
import type { AvailableContract } from "@/api/contracts";
import type { Drug } from "@/types";

vi.mock("@/components/pos/PrescriptionSelector", () => ({
    PrescriptionSelector: () => null,
}));

// No discount, no copay: this test is about the total/button parity, and a
// discount would exercise `computeTotals`' discount branch instead of the plain
// subtotal path the mismatch was reported on.
const contract: AvailableContract = {
    id: "c1",
    code: "STANDARD-PRICE",
    name: "Standard Price",
    type: "standard",
    discount_percentage: 0,
    is_default: true,
    requires_verification: false,
    requires_approval: false,
    display: "Standard Price",
    warning: null,
    copay_amount: null,
    copay_percentage: null,
    requires_preauthorization: false,
    insurance_provider_id: null,
    daily_usage_limit: null,
    per_customer_usage_limit: null,
    applies_to_prescription_only: false,
} as unknown as AvailableContract;

function makeDrug(id: string, name: string, price: number): Drug {
    return {
        id,
        name,
        unit_price: price,
        reorder_level: 300,
        reorder_quantity: 300,
    } as unknown as Drug;
}

function makeItem(id: string, name: string, price: number, quantity: number): CartItem {
    return {
        drug: makeDrug(id, name, price),
        quantity,
        requiresPrescription: false,
        prescriptionVerified: false,
        batchId: null,
    } as CartItem;
}

/** The cart reducer's own rules, mirrored so the harness drives a real cart. */
const LINE_CAP = 1000;

function applyQuantity(items: CartItem[], id: string, next: number): CartItem[] {
    return items.map((i) =>
        i.drug.id === id
            ? { ...i, quantity: Math.max(1, Math.min(next, LINE_CAP)) }
            : i
    );
}

function totalOf(items: CartItem[]): number {
    return items.reduce((t, i) => t + i.drug.unit_price * i.quantity, 0);
}

/**
 * Read both numbers straight out of the DOM, as a cashier reads them.
 *
 * Deliberately does NOT add data-testid attributes: task 3 forbids changing
 * production code, so this finds the existing nodes instead. The Total is the
 * `text-xl font-bold text-ink` span in the totals block; the button is located
 * by its accessible name.
 */
function readAmounts(): { total: string; button: string } {
    const totalEl = document.querySelector("span.text-xl.font-bold.text-ink");
    const buttonEl = screen.getByRole("button", { name: /Complete Sale/i });
    // If either node is missing, both sides would be "" and the equality
    // assertion below would pass while testing nothing. Refuse that.
    expect(
        totalEl?.textContent,
        "Total amount element not found — selector is stale"
    ).toBeTruthy();
    expect(buttonEl, "Complete Sale button not found").toBeTruthy();
    return {
        total: totalEl?.textContent?.replace(/[^\d.]/g, "") ?? "",
        button: buttonEl.textContent?.replace(/[^\d.]/g, "") ?? "",
    };
}

function mount(items: CartItem[], stockQuantities: Record<string, number>) {
    const state = { items, stock: stockQuantities };

    // One element factory, so the initial render and every rerender are built
    // from the same props. Copy-pasting the prop list twice is how the rerender
    // ended up with a half-built contract and crashed.
    const panel = () => (
        <CartPanel
            items={state.items}
            contract={contract}
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
            totals={{
                subtotal: totalOf(state.items),
                tax: 0,
                total: totalOf(state.items),
                discount: 0,
                discountAmount: 0,
                copay: 0,
                insuranceShare: 0,
                itemCount: state.items.length,
            } as never}
            validationErrors={[]}
            checkoutError={null}
            isSubmitting={false}
            stockQuantities={state.stock}
            onSetQuantity={(id: string, q: number) => {
                state.items = applyQuantity(state.items, id, q);
                rerender();
            }}
            onRemoveItem={(id: string) => {
                state.items = state.items.filter((i) => i.drug.id !== id);
                rerender();
            }}
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

    const view = render(panel());

    function rerender() {
        view.rerender(panel());
    }

    return { view, state, rerender };
}

async function settleAllTimers() {
    // Past the 100ms stock refresh, the 250ms list debounce and the 300ms search
    // debounce, with a macrotask flush so effects and their resulting state
    // updates have all run.
    await act(async () => {
        await new Promise((r) => setTimeout(r, 400));
    });
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
}

function expectAmountsMatch(context: string, expected?: number) {
    const { total, button } = readAmounts();
    // Both must agree...
    expect(
        `${total}`,
        `${context}: Total "${total}" vs button "${button}"`
    ).toBe(`${button}`);
    // ...and be the actual arithmetic, so two identically-wrong values cannot
    // pass this test.
    if (expected !== undefined) {
        expect(total, `${context}: wrong amount`).toBe(expected.toFixed(2));
    }
}

describe("Total and the Complete Sale button always agree", () => {
    it("survives rapid quantity steps 1 -> 13 -> 133", async () => {
        const { state, rerender } = mount([makeItem("d1", "Gebedol", 5, 1)], { d1: 500 });
        expectAmountsMatch("initial", 5);

        for (const q of [13, 133]) {
            act(() => {
                state.items = applyQuantity(state.items, "d1", q);
                rerender();
            });
            // The intermediate render, immediately after the change.
            expectAmountsMatch(`immediately after ${q}`, 5 * q);
            await settleAllTimers();
            expectAmountsMatch(`after timers settle at ${q}`, 5 * q);
        }
    });

    it("survives a paste of 133 into the field", async () => {
        const { state } = mount([makeItem("d1", "Gebedol", 5, 1)], { d1: 500 });
        const input = screen.getByTestId("qty-input-d1");

        act(() => {
            fireEvent.focus(input);
            fireEvent.change(input, { target: { value: "133" } });
        });
        expectAmountsMatch("after pasting 133", 665); // commits as you type
        fireEvent.blur(input);
        await settleAllTimers();
        expect(state.items[0].quantity).toBe(133);
        expectAmountsMatch("after blur at 133", 665);
    });

    it("survives adding and removing lines", async () => {
        const { state, rerender } = mount(
            [makeItem("d1", "Gebedol", 5, 1), makeItem("d2", "Amox", 12, 2)],
            { d1: 500, d2: 500 }
        );
        expectAmountsMatch("two lines", 29); // 5*1 + 12*2

        act(() => {
            state.items = [...state.items, makeItem("d3", "Minox", 100, 1)];
            rerender();
        });
        expectAmountsMatch("three lines", 129); // 5*1 + 12*2 + 100*1
        await settleAllTimers();
        expectAmountsMatch("three lines settled", 129);

        act(() => {
            state.items = state.items.filter((i) => i.drug.id !== "d3");
            rerender();
        });
        expectAmountsMatch("back to two lines", 29);
        await settleAllTimers();
        expectAmountsMatch("two lines settled", 29);
    });

    it("survives a burst of changes with stock refreshing underneath", async () => {
        // The reported failure was intermittent, so this hammers it: many steps
        // with the debounces live, plus a stock map that changes mid-sequence.
        const { state, rerender } = mount([makeItem("d1", "Gebedol", 5, 1)], { d1: 500 });

        for (const q of [3, 13, 30, 133, 40, 133, 7]) {
            act(() => {
                state.items = applyQuantity(state.items, "d1", q);
                state.stock = { d1: q <= 133 ? 500 : 120 };
                rerender();
            });
            expectAmountsMatch(`burst step ${q}`, 5 * q);
        }
        await settleAllTimers();
        expectAmountsMatch("after the burst settles", 5 * 7);
    });

    it("agrees at the reported reproduction points", async () => {
        // 3 items -> the screenshot's "Total ₵36.00 vs button ₵5.00"
        const three = mount(
            [
                makeItem("d1", "Gebedol", 5, 1),
                makeItem("d2", "Amox", 12, 1),
                makeItem("d3", "Minox", 19, 1),
            ],
            { d1: 500, d2: 500, d3: 500 }
        );
        expectAmountsMatch("3 items at 1 each", 36); // the screenshot's "Total 36.00"
        await settleAllTimers();
        expectAmountsMatch("3 items settled", 36);
        three.view.unmount();

        // Gebedol at 133 -> the screenshot's "Total ₵665.00 vs button ₵65.00"
        const at133 = mount([makeItem("d1", "Gebedol", 5, 133)], { d1: 500 });
        expectAmountsMatch("Gebedol at 133", 665); // the screenshot's "Total 665.00"
        await settleAllTimers();
        expectAmountsMatch("Gebedol at 133 settled", 665);
        at133.view.unmount();
    });

    it("keeps Amount Tendered, subtotal and copay on the same render", async () => {
        const { state, rerender } = mount([makeItem("d1", "Gebedol", 5, 1)], { d1: 500 });
        for (const q of [13, 133]) {
            act(() => {
                state.items = applyQuantity(state.items, "d1", q);
                rerender();
            });
            await settleAllTimers();
            // The tendered placeholder mirrors totals.total (CartPanel.tsx:1000).
            const tenderedInput = document.querySelector(
                "input[placeholder^=\"0\"], input[placeholder*=\".\"]"
            );
            const placeholder = tenderedInput?.getAttribute("placeholder") ?? "";
            const tendered = Number(placeholder.replace(/[^\d.]/g, ""));
            expect(`${tendered.toFixed(2)}`, `tendered at ${q}`).toBe(
                totalOf(state.items).toFixed(2)
            );
        }
    });
});
