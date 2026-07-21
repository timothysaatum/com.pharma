/** @vitest-environment jsdom */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import POSPage from "@/pages/POSPage";

const mocks = vi.hoisted(() => ({
  processSale: vi.fn(),
  recordSaleTransaction: vi.fn(),
  verifyEligibility: vi.fn(),
  emit: vi.fn(),
  shouldFallback: vi.fn(() => true),
}));

const contract = {
  id: "contract-1",
  code: "STANDARD",
  name: "Standard",
  type: "standard",
  discount_percentage: 0,
  is_default: true,
  requires_verification: false,
  requires_approval: false,
  display: "Standard",
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

const cartItem = {
  drug: {
    id: "drug-1",
    name: "Test Drug",
    sku: "TEST-1",
    generic_name: "Test",
    usage_instructions: null,
    unit_price: 10,
    tax_rate: 0,
  },
  quantity: 2,
  batchId: null,
  requiresPrescription: false,
  prescriptionVerified: false,
};

const cart = {
  state: {
    items: [cartItem],
    contract,
    customerName: "Walk-in",
    customerId: null,
    paymentMethod: "cash",
    amountPaid: 20,
    prescriptionId: null,
    insuranceClaimNumber: "",
    insurancePreAuthNumber: "",
    insuranceVerified: false,
    notes: "",
  },
  totals: {
    subtotal: 20,
    discountAmount: 0,
    taxAmount: 0,
    total: 20,
    change: 0,
    patientCopay: 0,
    amountDue: 20,
    itemCount: 2,
  },
  validationErrors: [],
  isValid: true,
  addItem: vi.fn(),
  setQuantity: vi.fn(),
  removeItem: vi.fn(),
  setPrescriptionVerified: vi.fn(),
  setContract: vi.fn(),
  setCustomerId: vi.fn(),
  setCustomerName: vi.fn(),
  setPaymentMethod: vi.fn(),
  setAmountPaid: vi.fn(),
  setSplitPayment: vi.fn(),
  setPrescriptionId: vi.fn(),
  setInsuranceClaimNumber: vi.fn(),
  setInsurancePreAuthNumber: vi.fn(),
  setInsuranceVerified: vi.fn(),
  setNotes: vi.fn(),
  clearCart: vi.fn(),
  setStockQuantities: vi.fn(),
  buildSaleCreate: vi.fn(() => ({
    branch_id: "branch-1",
    price_contract_id: "contract-1",
    customer_name: "Walk-in",
    items: [{
      drug_id: "drug-1",
      quantity: 2,
      requires_prescription: false,
      prescription_verified: false,
    }],
    payment_method: "cash",
    amount_paid: 20,
    insurance_verified: false,
  })),
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    activeBranchId: "branch-1",
    user: {
      id: "user-1",
      full_name: "Cashier One",
      organization_id: "org-1",
      is_super_admin: false,
      roles: [{ name: "Cashier" }],
    },
  }),
}));
vi.mock("@/hooks/useSyncStatus", () => ({ useSyncStatus: () => ({ status: "idle" }) }));
vi.mock("@/hooks/useOrganization", () => ({ useOrganization: () => ({ org: { settings: {} } }) }));
vi.mock("@/hooks/useCart", () => ({ useCart: () => cart }));
vi.mock("@/api/contracts", () => ({
  contractsApi: {
    getAvailableForPos: vi.fn(async () => [contract]),
    verifyEligibility: mocks.verifyEligibility,
  },
}));
vi.mock("@/api/sales", () => ({ salesApi: { processSale: mocks.processSale } }));
vi.mock("@/api/stats", () => ({ statsApi: { getBranchStats: vi.fn(async () => ({ total_sales_today: 0 })) } }));
vi.mock("@/api/client", () => ({
  isBackendReachable: () => true,
  isOfflineError: () => true,
  parseApiError: (error: unknown) => error instanceof Error ? error.message : String(error),
}));
vi.mock("@/lib/localRead", () => ({
  localRead: {
    searchSales: vi.fn(async () => ({ items: [] })),
    getAvailableContractsForPos: vi.fn(async () => [contract]),
    getSellableQuantity: vi.fn(async () => ({ sellable: 100, notStocked: false })),
  },
}));
vi.mock("@/lib/offlineSalesManager", () => ({
  offlineSalesManager: { recordSaleTransaction: mocks.recordSaleTransaction },
}));
vi.mock("@/lib/offlineFallback", () => ({
  shouldUseOfflineSalePath: () => false,
  shouldFallbackToOfflineSaleAfterError: mocks.shouldFallback,
}));
vi.mock("@/lib/events", () => ({
  appEvents: { emit: mocks.emit },
  useAppEvent: vi.fn(),
}));
vi.mock("@/components/pos/DrugSearchPanel", () => ({ DrugSearchPanel: () => null }));
vi.mock("@/components/pos/CartPanel", () => ({
  CartPanel: ({ onCheckout, isSubmitting }: { onCheckout: () => void; isSubmitting: boolean }) => (
    <button type="button" onClick={onCheckout} data-submitting={String(isSubmitting)}>
      Complete Sale
    </button>
  ),
}));
vi.mock("@/components/pos/SaleSuccessModal", () => ({ SaleSuccessModal: () => <div>Sale complete</div> }));
vi.mock("framer-motion", () => ({ AnimatePresence: ({ children }: { children: ReactNode }) => children }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

describe("POS checkout resilience", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window.navigator, "onLine", { configurable: true, value: true });
    mocks.verifyEligibility.mockResolvedValue({ eligible: true, message: "Eligible" });
    mocks.recordSaleTransaction.mockResolvedValue({ success: true, saleId: "ignored" });
  });

  it("uses the online checkout ID for fallback after a lost response", async () => {
    mocks.processSale.mockRejectedValueOnce(new Error("Network response lost"));
    render(<POSPage />);

    fireEvent.click(await screen.findByRole("button", { name: "Complete Sale" }));

    await waitFor(() => expect(mocks.recordSaleTransaction).toHaveBeenCalledOnce());
    const onlinePayload = mocks.processSale.mock.calls[0][0];
    const [offlineSale, _items, inventoryDeltas, idempotencyKey] =
      mocks.recordSaleTransaction.mock.calls[0];
    expect(onlinePayload.client_sale_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(offlineSale.id).toBe(onlinePayload.client_sale_id);
    expect(idempotencyKey).toBe(`sale:${onlinePayload.client_sale_id}`);
    expect(inventoryDeltas).toEqual([{ drug_id: "drug-1", delta: -2 }]);
    expect(await screen.findByText("Sale complete")).toBeTruthy();
  });

  it("blocks rapid double submission before React can rerender the button", async () => {
    let resolveSale!: (value: unknown) => void;
    mocks.processSale.mockImplementationOnce(() => new Promise((resolve) => {
      resolveSale = resolve;
    }));
    render(<POSPage />);
    const button = await screen.findByRole("button", { name: "Complete Sale" });

    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(mocks.processSale).toHaveBeenCalledOnce());

    resolveSale({ success: true, sale: { id: "server-sale" } });
    await screen.findByText("Sale complete");
    expect(mocks.recordSaleTransaction).not.toHaveBeenCalled();
  });
});
