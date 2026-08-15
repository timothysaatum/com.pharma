/** @vitest-environment jsdom */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AvailableContract } from "@/api/contracts";
import { apiClient } from "@/api/client";
import { CartPanel } from "@/components/pos/CartPanel";
import { localRead } from "@/lib/localRead";
import type { CartItem, CartTotals } from "@/hooks/useCart";
import type { Drug } from "@/types";

let backendReachable = false;

vi.mock("@/api/client", () => ({
  apiClient: {
    get: vi.fn(),
  },
  isBackendReachable: () => backendReachable,
  isBackendKnownUnreachable: () => !backendReachable,
}));

vi.mock("@/lib/localRead", () => ({
  localRead: {
    searchCustomerMatches: vi.fn(),
  },
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({
      user: {
        organization_id: "org-abc",
      },
    }),
}));

vi.mock("@/components/pos/PrescriptionSelector", () => ({
  PrescriptionSelector: () => null,
}));

const corporateContract: AvailableContract = {
  id: "contract-corporate",
  code: "CORP",
  name: "Corporate Contract",
  type: "corporate",
  discount_percentage: 10,
  is_default: true,
  requires_verification: false,
  requires_approval: false,
  display: "Corporate Contract",
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

const drug = {
  id: "drug-1",
  name: "Paracetamol",
  strength: "500mg",
  unit_price: 10,
  tax_rate: 0,
  requires_prescription: false,
} as Drug;

const item: CartItem = {
  drug,
  quantity: 1,
  requiresPrescription: false,
  prescriptionVerified: false,
  batchId: null,
};

const totals: CartTotals = {
  subtotal: 10,
  discountAmount: 1,
  taxAmount: 0,
  total: 9,
  change: 0,
  patientCopay: 0,
  amountDue: 9,
  itemCount: 1,
};

function renderCartPanel(overrides: Partial<React.ComponentProps<typeof CartPanel>> = {}) {
  const props: React.ComponentProps<typeof CartPanel> = {
    items: [item],
    contract: corporateContract,
    contracts: [corporateContract],
    contractsLoading: false,
    customerName: "",
    customerId: null,
    paymentMethod: "credit",
    amountPaid: 0,
    prescriptionId: null,
    insuranceClaimNumber: "",
    insurancePreAuthNumber: "",
    insuranceVerified: false,
    notes: "",
    totals,
    validationErrors: [],
    checkoutError: null,
    isSubmitting: false,
    onSetQuantity: vi.fn(),
    onRemoveItem: vi.fn(),
    onSetPrescriptionVerified: vi.fn(),
    onSetContract: vi.fn(),
    onSetCustomerId: vi.fn(),
    onSetCustomerName: vi.fn(),
    onSetPaymentMethod: vi.fn(),
    onSetAmountPaid: vi.fn(),
    onSetSplitPayment: vi.fn(),
    onSetPrescriptionId: vi.fn(),
    onSetInsuranceClaimNumber: vi.fn(),
    onSetInsurancePreAuthNumber: vi.fn(),
    onSetInsuranceVerified: vi.fn(),
    onSetNotes: vi.fn(),
    onCheckout: vi.fn(),
    onClearCart: vi.fn(),
    ...overrides,
  };

  render(<CartPanel {...props} />);
  return props;
}

describe("CartPanel customer search", () => {
  beforeEach(() => {
    backendReachable = false;
    vi.clearAllMocks();
  });

  it("shows offline customer search results in the visible POS customer control", async () => {
    vi.mocked(localRead.searchCustomerMatches).mockResolvedValue([
      {
        id: "cust-123",
        full_name: "Cassie1 Nam",
        phone: "+2335555555",
        email: "cassie@example.com",
        loyalty_tier: "gold",
        has_insurance: true,
        contract_name: "Corporate Contract",
      },
    ]);
    const onSetCustomerId = vi.fn();
    const onSetCustomerName = vi.fn();

    renderCartPanel({ onSetCustomerId, onSetCustomerName });

    const input = screen.getByPlaceholderText(/search by name, phone, email/i);
    fireEvent.change(input, { target: { value: "cass" } });

    expect(await screen.findByText("Cassie1 Nam")).toBeTruthy();
    expect(screen.getByText(/\+2335555555/)).toBeTruthy();
    expect(localRead.searchCustomerMatches).toHaveBeenCalledWith("cass", 10, "org-abc");

    fireEvent.click(screen.getByText("Cassie1 Nam"));

    await waitFor(() => {
      expect(onSetCustomerName).toHaveBeenCalledWith("Cassie1 Nam");
      expect(onSetCustomerId).toHaveBeenCalledWith("cust-123");
    });
  });

  it("keeps local customer results when the online API returns no matches", async () => {
    backendReachable = true;
    vi.mocked(localRead.searchCustomerMatches).mockResolvedValue([
      {
        id: "cust-123",
        full_name: "Cassie1 Nam",
        phone: "+2335555555",
        email: "cassie@example.com",
        loyalty_tier: "gold",
        has_insurance: true,
        contract_name: "Corporate Contract",
      },
    ]);
    vi.mocked(apiClient.get).mockResolvedValue({ data: { matches: [] } });

    renderCartPanel();

    fireEvent.change(screen.getByPlaceholderText(/search by name, phone, email/i), {
      target: { value: "cass" },
    });

    expect(await screen.findByText("Cassie1 Nam")).toBeTruthy();
    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith(
        "/customers/search",
        expect.objectContaining({
          params: { q: "cass", limit: 10 },
        }),
      );
    });
  });

  it("shows local customers when offline without making an API call", async () => {
    vi.mocked(localRead.searchCustomerMatches).mockResolvedValue([
      {
        id: "cust-456",
        full_name: "Offline Customer",
        phone: "+2339999999",
        email: "offline@example.com",
        loyalty_tier: "silver",
        has_insurance: false,
        contract_name: null,
      },
    ]);

    // Simulate browser offline state
    const originalOnLine = navigator.onLine;
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });

    renderCartPanel();

    fireEvent.change(screen.getByPlaceholderText(/search by name, phone, email/i), {
      target: { value: "offline" },
    });

    expect(await screen.findByText("Offline Customer")).toBeTruthy();
    expect(screen.getByText(/\+2339999999/)).toBeTruthy();
    expect(localRead.searchCustomerMatches).toHaveBeenCalledWith("offline", 10, "org-abc");
    // API must NOT be called when browser reports offline
    expect(apiClient.get).not.toHaveBeenCalled();

    // Restore navigator.onLine for other tests
    Object.defineProperty(navigator, "onLine", { value: originalOnLine, configurable: true });
  });

  it("shows local customers when backend is known unreachable (browser online)", async () => {
    vi.mocked(localRead.searchCustomerMatches).mockResolvedValue([
      {
        id: "cust-789",
        full_name: "Fallback Customer",
        phone: "+2338888888",
        email: "fallback@example.com",
        loyalty_tier: "bronze",
        has_insurance: false,
        contract_name: null,
      },
    ]);

    // Navigtor reports online but the backend flag says unreachable
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
    backendReachable = false;

    renderCartPanel();

    fireEvent.change(screen.getByPlaceholderText(/search by name, phone, email/i), {
      target: { value: "fallback" },
    });

    expect(await screen.findByText("Fallback Customer")).toBeTruthy();
    expect(screen.getByText(/\+2338888888/)).toBeTruthy();
    expect(localRead.searchCustomerMatches).toHaveBeenCalledWith("fallback", 10, "org-abc");
    // API must NOT be called when backend is known unreachable
    expect(apiClient.get).not.toHaveBeenCalled();
  });
});

describe("CartPanel stock quantity resolution", () => {
  it("displays resolved stock quantity from stockQuantities map", () => {
    renderCartPanel({
      stockQuantities: { [item.drug.id]: 220 },
    });
    expect(screen.getByText("/220")).toBeTruthy();
  });

  it("falls back to drug valid_batch_quantity / available_quantity if not in stockQuantities", () => {
    const drugWithStock = {
      ...item.drug,
      valid_batch_quantity: 220,
    };
    renderCartPanel({
      items: [{ ...item, drug: drugWithStock as any }],
      stockQuantities: {},
    });
    expect(screen.getByText("/220")).toBeTruthy();
  });
});
