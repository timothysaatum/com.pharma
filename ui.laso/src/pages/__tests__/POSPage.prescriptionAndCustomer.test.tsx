/** @vitest-environment jsdom */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import POSPage from "@/pages/POSPage";

const { mockDb, mockRecordSaleTransaction } = vi.hoisted(() => ({
  mockDb: {
    select: vi.fn(),
    executeTransaction: vi.fn(),
  },
  mockRecordSaleTransaction: vi.fn(),
}));

vi.mock("@/lib/localDb", () => ({
  getDb: async () => mockDb,
}));

vi.mock("@/lib/offlineSalesManager", () => ({
  offlineSalesManager: { recordSaleTransaction: mockRecordSaleTransaction },
}));

const mockCart = {
  state: {
    items: [
      {
        drug: { id: "drug-1", name: "Amlodipine", unit_price: 10, strength: "5mg", tax_rate: 0 },
        quantity: 1,
        requiresPrescription: true,
        prescriptionVerified: true,
        batchId: "batch-1",
      }
    ],
    contract: { id: "contract-1", name: "Standard", type: "standard", discount_percentage: 0 },
    customerName: "Alice Smith",
    customerId: "cust-123",
    paymentMethod: "cash",
    amountPaid: 10,
    prescriptionId: "rx-123",
    insuranceClaimNumber: "",
    insurancePreAuthNumber: "",
    insuranceVerified: false,
    notes: "",
  },
  totals: {
    subtotal: 10,
    discountAmount: 0,
    taxAmount: 0,
    total: 10,
    change: 0,
    patientCopay: 0,
    amountDue: 10,
    itemCount: 1,
  },
  validationErrors: [],
  isValid: true,
  buildSaleCreate: () => ({
    branch_id: "branch-1",
    price_contract_id: "contract-1",
    customer_id: "cust-123",
    items: [
      {
        drug_id: "drug-1",
        quantity: 1,
        batch_id: "batch-1",
        requires_prescription: true,
        prescription_verified: true,
      }
    ],
    payment_method: "cash",
    amount_paid: 10,
    prescription_id: "rx-123",
    insurance_verified: false,
  }),
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
};

vi.mock("@/hooks/useCart", () => ({
  useCart: () => mockCart,
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    activeBranchId: "branch-1",
    user: {
      id: "user-1",
      full_name: "Josh N",
      organization_id: "org-abc",
      is_super_admin: false,
      roles: [{ name: "Admin" }],
    },
  }),
}));

vi.mock("@/hooks/useSyncStatus", () => ({
  useSyncStatus: () => ({ status: "offline" }),
}));

vi.mock("@/hooks/useOrganization", () => ({
  useOrganization: () => ({ org: { settings: {} } }),
}));

vi.mock("@/api/contracts", () => ({
  contractsApi: {
    getAvailableForPos: vi.fn(),
    verifyEligibility: vi.fn(),
  },
}));

vi.mock("@/api/sales", () => ({
  salesApi: { processSale: vi.fn() },
}));

vi.mock("@/api/stats", () => ({
  statsApi: { getBranchStats: vi.fn() },
}));

vi.mock("@/api/client", () => ({
  isBackendReachable: () => false,
  isOfflineError: () => true,
  parseApiError: (err: unknown) => err instanceof Error ? err.message : String(err),
}));

vi.mock("@/lib/localRead", () => ({
  localRead: {
    searchSales: vi.fn().mockResolvedValue({ items: [] }),
    getAvailableContractsForPos: vi.fn().mockResolvedValue([]),
    getSellableQuantity: vi.fn().mockResolvedValue({ sellable: 10, notStocked: false }),
  },
}));

vi.mock("@/lib/offlineFallback", () => ({
  shouldFallbackToOfflineSaleAfterError: () => false,
  shouldUseOfflineSalePath: () => true,
}));

vi.mock("@/lib/events", () => ({
  appEvents: { emit: vi.fn() },
  useAppEvent: vi.fn(),
}));

vi.mock("@/components/pos/DrugSearchPanel", () => ({
  DrugSearchPanel: () => <div>Drug search</div>,
}));

vi.mock("@/components/pos/CartPanel", () => ({
  CartPanel: ({ onCheckout, checkoutError }: any) => (
    <div>
      {checkoutError && <div data-testid="checkout-error">{checkoutError}</div>}
      <button data-testid="complete-sale-btn" onClick={onCheckout}>Complete Sale</button>
    </div>
  ),
}));

vi.mock("@/components/pos/SaleSuccessModal", () => ({
  SaleSuccessModal: () => null,
}));

vi.mock("framer-motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

describe("POSPage prescription validation and customer name", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should block checkout and display error when prescription medications do not match cart drug", async () => {
    mockDb.select.mockResolvedValue([
      {
        medications: JSON.stringify([{ drug_id: "different-drug-id" }]),
        refills_remaining: 3,
        status: "active",
        prescription_number: "RX-111",
        prescriber_name: "Dr. Who",
        prescriber_license: "LIC-777",
      }
    ]);

    render(<POSPage />);

    const button = screen.getByTestId("complete-sale-btn");
    fireEvent.click(button);

    await screen.findByTestId("checkout-error");
    expect(screen.getByTestId("checkout-error").textContent).toBe(
      "Selected prescription is not valid for all prescription-required drugs in the cart. Missing: Amlodipine"
    );
    expect(mockRecordSaleTransaction).not.toHaveBeenCalled();
  });

  it("should succeed and populate customer and prescription details on offlineSale when prescription is valid", async () => {
    mockDb.select.mockResolvedValue([
      {
        medications: JSON.stringify([{ drug_id: "drug-1" }]),
        refills_remaining: 3,
        status: "active",
        prescription_number: "RX-111",
        prescriber_name: "Dr. Who",
        prescriber_license: "LIC-777",
      }
    ]);

    mockRecordSaleTransaction.mockResolvedValue({ success: true, saleId: "sale-123" });

    render(<POSPage />);

    const button = screen.getByTestId("complete-sale-btn");
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockRecordSaleTransaction).toHaveBeenCalled();
    });

    const [offlineSale, saleItems] = mockRecordSaleTransaction.mock.calls[0];

    // Assert customer and prescriber details are populated
    expect(offlineSale.customer_name).toBe("Alice Smith");
    expect(offlineSale.prescription_number).toBe("RX-111");
    expect(offlineSale.prescriber_name).toBe("Dr. Who");
    expect(offlineSale.prescriber_license).toBe("LIC-777");

    // Assert item-level prescription_id is populated
    expect(saleItems[0].prescription_id).toBe("rx-123");
  });
});
