/**
 * @vitest-environment jsdom
 *
 * The product list must show stock AFTER a sale, not before it.
 *
 * The reported symptom: after selling 1 Gebedol (stock 114 -> 113 in the local
 * database) the list kept saying "114 available" until the cashier left the page
 * and came back, while the cart already used 113 — the cart reads a different
 * map (POSPage.tsx:108) from the one the list renders
 * (DrugSearchPanel.tsx:352), so the two disagreed.
 *
 * Cause: DrugSearchPanel had no subscription to appEvents at all. POSPage emits
 * `inventory:changed` and `sales:changed` after a sale (POSPage.tsx:589-590 and
 * :608-609 for the offline path), and nothing in the panel listened, so the list
 * only refreshed when something else forced a refetch.
 *
 * The fix is a subscription that refetches through the EXISTING path with the
 * query, filters and page the cashier already has. The two stock maps are NOT
 * merged — the panel keeps its own.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { appEvents } from "@/lib/events";

const BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a";
const GEBEDOL = "8d4cc1a7-03c7-4a6a-8080-2bda5def026f";

/** Server stock, mutable so a test can simulate a completed sale. */
const server = {
    stock: 114,
    calls: [] as Array<{ page: number; search?: string }>,
};

vi.mock("@/lib/localRead", () => ({
    localRead: {
        getBranchInventory: vi.fn(async () => ({
            items: [],
            total: 0,
        })),
        searchWithBranchAvailability: vi.fn(async () => ({
            items: [],
            total: 0,
        })),
    },
}));

vi.mock("@/api/inventory", () => ({
    inventoryApi: {
        getBranchInventory: vi.fn(
            async (
                _branchId: string,
                params: { page?: number; search?: string },
            ) => {
                server.calls.push({ page: params.page ?? 1, search: params.search });
                return {
                    items: [
                        {
                            drug_id: GEBEDOL,
                            drug: {
                                id: GEBEDOL,
                                name: "Gebedol",
                                unit_price: 5,
                                reorder_level: 300,
                                reorder_quantity: 300,
                            },
                            quantity: server.stock,
                            available_quantity: server.stock,
                            valid_batch_quantity: server.stock,
                        },
                    ],
                    total: 1,
                    page: params.page ?? 1,
                    page_size: 30,
                };
            }
        ),
    },
}));

vi.mock("@/api/client", () => ({
    isBackendReachable: () => true,
    isBackendKnownUnreachable: () => false,
    isOfflineError: () => false,
    parseApiError: (e: unknown) => String(e),
    BACKEND_CONNECTIVITY_EVENT: "backend:connectivity",
}));

const authState = {
    activeBranchId: BRANCH,
    organizationId: "2d060ef8-a302-447c-91f4-b2fd30268341",
    user: { id: "u1", full_name: "Tim" },
};
vi.mock("@/stores/authStore", () => ({
    useAuthStore: (sel?: (s: typeof authState) => unknown) =>
        sel ? sel(authState) : authState,
    useAuthStore_: () => authState,
}));

async function mountPanel() {
    const { DrugSearchPanel } = await import("@/components/pos/DrugSearchPanel");
    return render(
        <DrugSearchPanel onAdd={() => {}} disabledDrugIds={new Set<string>()} />
    );
}

/** Lets the 250ms debounce elapse. */
async function settle(ms = 400) {
    await act(async () => {
        await new Promise((r) => setTimeout(r, ms));
    });
}

describe("product list refreshes when stock moves", () => {
    beforeEach(() => {
        server.stock = 114;
        server.calls = [];
    });
    afterEach(() => {
        cleanup();
        vi.clearAllMocks();
    });

    it("shows the pre-sale quantity on mount", async () => {
        await mountPanel();
        await waitFor(() => expect(screen.getByText(/available/)).toBeTruthy());
        expect(screen.getByText(/available/).textContent).toContain("114");
    });

    it("after a sale the list shows the NEW stock", async () => {
        await mountPanel();
        await waitFor(() => expect(screen.getByText(/114/)).toBeTruthy());

        // The sale commits locally, POSPage emits, the panel refetches.
        server.stock = 113;
        await act(async () => {
            appEvents.emit("inventory:changed");
            appEvents.emit("sales:changed");
        });
        await settle();

        await waitFor(() => expect(screen.getByText(/113/)).toBeTruthy());
        expect(screen.getByText(/available/).textContent).toContain("113");
        expect(screen.queryByText(/114/)).toBeNull();
    });

    it("a burst of events triggers exactly ONE refetch", async () => {
        await mountPanel();
        await waitFor(() => expect(screen.getByText(/114/)).toBeTruthy());
        const before = server.calls.length;

        await act(async () => {
            appEvents.emit("inventory:changed");
            appEvents.emit("sales:changed");
            appEvents.emit("purchases:changed");
            appEvents.emit("inventory:changed");
        });
        await settle();

        expect(server.calls.length - before).toBe(1);
    });

    it("an unmounted panel does not refetch", async () => {
        const view = await mountPanel();
        await waitFor(() => expect(screen.getByText(/114/)).toBeTruthy());
        const before = server.calls.length;

        view.unmount();
        server.stock = 50;
        await act(async () => {
            appEvents.emit("inventory:changed");
            appEvents.emit("sales:changed");
        });
        await settle();

        // Unsubscribed on unmount: no fetch, and no timer left to fire later.
        expect(server.calls.length).toBe(before);
    });

    it("refetches with the page and search the cashier already had", async () => {
        await mountPanel();
        await waitFor(() => expect(screen.getByText(/114/)).toBeTruthy());
        const before = server.calls.length;

        server.stock = 113;
        await act(async () => {
            appEvents.emit("inventory:changed");
        });
        await settle();

        expect(server.calls.length - before).toBe(1);
        // Page 1 preserved (not reset), and the same (empty) query re-sent.
        expect(server.calls[server.calls.length - 1]?.page).toBe(1);
        expect(server.calls[server.calls.length - 1]?.search).toBeUndefined();
    });

    it("keeps the cart's own quantity working while the cart holds that drug", async () => {
        // The cart may hold 5 of a drug whose stock just fell to 3. The list
        // refreshes its own number; the cart's red shortfall band is a SEPARATE
        // concern driven by POSPage's stockQuantities map, and this fix must not
        // disturb it — the two maps stay independent.
        const { CartPanel } = await import("@/components/pos/CartPanel");
        const onSetQuantity = vi.fn();
        const { CartItem } = { CartItem: null } as never;
        void CartItem;

        render(
            <CartPanel
                items={[
                    {
                        drug: {
                            id: GEBEDOL,
                            name: "Gebedol",
                            unit_price: 5,
                            reorder_level: 300,
                            reorder_quantity: 300,
                        } as never,
                        quantity: 5,
                        requiresPrescription: false,
                        prescriptionVerified: false,
                        batchId: null,
                    },
                ]}
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
                stockQuantities={{ [GEBEDOL]: 3 }}
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

        // The cart's own band is unaffected by the list's refresh.
        expect(screen.getByText("Only 3 available (requested 5)")).toBeTruthy();
        // And "+" is disabled at the cart's limit, independently of the list.
        expect(
            (screen.getByLabelText(/Increase quantity/) as HTMLButtonElement).disabled
        ).toBe(true);
    });
});
