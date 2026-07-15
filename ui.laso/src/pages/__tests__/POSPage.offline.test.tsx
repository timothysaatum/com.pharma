/** @vitest-environment jsdom */
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { contractsApi } from "@/api/contracts";
import { localRead } from "@/lib/localRead";
import POSPage from "@/pages/POSPage";

const { localContract } = vi.hoisted(() => ({
  localContract: {
    id: "contract-standard",
    code: "STANDARD",
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
    applies_to_otc: true,
    minimum_purchase_amount: null,
    maximum_purchase_amount: null,
  },
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

vi.mock("@/hooks/useCart", () => ({
  useCart: () => ({
    state: {
      items: [],
      contract: null,
      customerName: "",
      customerId: null,
      paymentMethod: "cash",
      amountPaid: 0,
      prescriptionId: null,
      insuranceClaimNumber: "",
      insurancePreAuthNumber: "",
      insuranceVerified: false,
      notes: "",
    },
    totals: {
      subtotal: 0,
      discountAmount: 0,
      taxAmount: 0,
      total: 0,
      change: 0,
      patientCopay: 0,
      amountDue: 0,
      itemCount: 0,
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
    buildSaleCreate: vi.fn(),
    clearCart: vi.fn(),
  }),
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
  isBackendReachable: () => true,
  isOfflineError: () => true,
  parseApiError: (err: unknown) => err instanceof Error ? err.message : String(err),
}));

vi.mock("@/lib/localRead", () => ({
  localRead: {
    searchSales: vi.fn().mockResolvedValue({ items: [] }),
    getAvailableContractsForPos: vi.fn().mockResolvedValue([localContract]),
  },
}));

vi.mock("@/lib/offlineSalesManager", () => ({
  offlineSalesManager: { recordSaleTransaction: vi.fn() },
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
  CartPanel: ({ contracts, contractsLoading }: { contracts: typeof localContract[]; contractsLoading: boolean }) => (
    <div>
      <div>{contractsLoading ? "contracts-loading" : "contracts-ready"}</div>
      {contracts.map((contract) => (
        <div key={contract.id}>{contract.display}</div>
      ))}
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

describe("POSPage offline contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads POS contracts from SQLite while sync status is offline", async () => {
    render(<POSPage />);

    await screen.findByText("Standard Price");

    await waitFor(() => {
      expect(localRead.getAvailableContractsForPos).toHaveBeenCalledWith("branch-1", "org-abc");
    });
    expect(contractsApi.getAvailableForPos).not.toHaveBeenCalled();
  });
});
