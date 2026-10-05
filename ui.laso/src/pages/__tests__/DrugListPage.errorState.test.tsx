/**
 * @vitest-environment jsdom
 *
 * Phase 1 regression tests: a failed catalogue fetch must not read as an empty
 * formulary.
 *
 * The bug: on a non-offline error the page set `error` AND left `drugs` at its
 * previous value. With a cold start that produced a red banner above
 * "No drugs found. Add your first drug to get started." — telling the owner
 * their org had no drugs when the truth was that the request failed. A 500 was
 * silently indistinguishable from an empty result.
 *
 * Now: the error state renders its own body with a Retry action, and the empty
 * state is reserved for a genuinely empty successful result.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockDrugList, mockSearchDrugs } = vi.hoisted(() => ({
  mockDrugList: vi.fn(),
  mockSearchDrugs: vi.fn(),
}));

const mockUser = {
  id: "user-1",
  full_name: "Inventory Manager",
  organization_id: "org-1",
  is_super_admin: false,
  assigned_branches: ["branch-1"],
  roles: [{ id: "role-1", name: "Manager", level: 20, permissions: [] }],
  effective_permissions: {
    direct_role_permissions: [],
    inherited_permissions: ["manage_drugs"],
    effective_permissions: ["manage_drugs"],
    max_role_level: 20,
  },
};

vi.mock("@/api/drugs", () => ({
  drugApi: { list: mockDrugList, update: vi.fn() },
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({ user: mockUser, activeBranchId: "branch-1" }),
}));

vi.mock("@/hooks/useCategories", () => ({
  useCategoryTree: () => ({ tree: [], invalidate: vi.fn() }),
}));

// parseApiError surfaces the server's message verbatim.
vi.mock("@/api/client", () => ({
  isBackendReachable: () => true,
  isBackendKnownUnreachable: () => false,
  isOfflineOrUnreachable: () => false,
  parseApiError: (err: any) => err?.message ?? "Request failed",
}));

vi.mock("@/lib/localRead", () => ({
  localRead: { searchDrugs: mockSearchDrugs },
}));

// Mirrors the real wrapper, including the new additive `fallbackError` field.
// The wrapper catches EVERY server error and returns the local fallback, which
// is exactly why the reported 422 rendered as "No drugs found" with no signal
// that the request had failed.
vi.mock("@/lib/withTimeout", () => ({
  withTimeout: async (
    primary: () => Promise<unknown>,
    cache: () => Promise<unknown>
  ) => {
    try {
      return { data: await primary(), isFromCache: false };
    } catch (err) {
      try {
        const data = await cache();
        return {
          data,
          isFromCache: true,
          cached_at: new Date().toISOString(),
          fallbackError: err instanceof Error ? err : new Error(String(err)),
        };
      } catch {
        throw err;
      }
    }
  },
}));

vi.mock("@/lib/events", () => ({
  appEvents: { emit: vi.fn() },
  useAppEvent: vi.fn(),
}));

vi.mock("@/components/DataFreshnessIndicator", () => ({
  DataFreshnessIndicator: () => null,
}));

vi.mock("@/components/drugs/DrugForm", () => ({
  DrugForm: () => <div>Drug form</div>,
}));
vi.mock("@/components/inventory/AddBatchForm", () => ({
  AddBatchForm: () => <div>Add batch form</div>,
}));
vi.mock("@/components/drugs/DrugCategoryModal", () => ({
  DrugCategoryModal: () => <div>Category modal</div>,
}));
vi.mock("@/components/drugs/DrugImportWizard", () => ({
  DrugImportWizard: () => <div>Import wizard</div>,
}));

import DrugListPage from "../DrugListPage";

const ONE_DRUG = {
  items: [
    {
      id: "drug-1",
      name: "Paracetamol",
      generic_name: "Acetaminophen",
      sku: "PAR-500",
      drug_type: "otc",
      requires_prescription: false,
      unit_price: 10,
      is_active: true,
    },
  ],
  total: 1,
  page: 1,
  page_size: 20,
  total_pages: 1,
};

describe("DrugListPage error state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: the local fallback has nothing, which is what the live device
    // did — it produced an empty catalogue and the page said "No drugs found".
    mockSearchDrugs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20, total_pages: 0 });
  });

  it("shows an explicit error state with Retry when the fetch fails", async () => {
    mockDrugList.mockRejectedValue(
      Object.assign(new Error("The server could not build the response for this request."), {
        isAxiosError: true,
        response: { status: 500, data: { detail: "server fault" } },
      })
    );

    render(<DrugListPage />);

    // The error body, not the empty state.
    expect(await screen.findByText(/could not load the drug catalogue/i)).toBeTruthy();
    expect(screen.getByText(/request failure, not an empty formulary/i)).toBeTruthy();

    // The empty state — which claims "no drugs" and offers "Add Drug" — must
    // NOT be rendered on top of a failed request.
    expect(screen.queryByText(/no drugs found/i)).toBeNull();
  });

  it("surfaces the server's message in the banner", async () => {
    mockDrugList.mockRejectedValue(
      Object.assign(new Error("boom: reorder_quantity"), {
        isAxiosError: true,
        response: { status: 500, data: { detail: "boom: reorder_quantity" } },
      })
    );

    render(<DrugListPage />);
    expect(await screen.findByText(/boom: reorder_quantity/)).toBeTruthy();
  });

  it("offers a Retry action that re-issues the request", async () => {
    mockDrugList.mockRejectedValue(new Error("transient"));
    render(<DrugListPage />);

    const retry = await screen.findByRole("button", { name: /retry/i });
    expect(mockDrugList).toHaveBeenCalledTimes(1);

    // Second attempt succeeds.
    mockDrugList.mockResolvedValue(ONE_DRUG);
    mockSearchDrugs.mockResolvedValue({
      items: ONE_DRUG.items,
      total: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
    });
    retry.click();

    await waitFor(() => {
      expect(mockDrugList).toHaveBeenCalledTimes(2);
    });
    // The error state clears and the table renders.
    await waitFor(() => {
      expect(screen.queryByText(/could not load the drug catalogue/i)).toBeNull();
    });
    expect(await screen.findByText("Paracetamol")).toBeTruthy();
  });

  it("still shows the genuine empty state when the request succeeds with no drugs", async () => {
    mockDrugList.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
      total_pages: 0,
    });

    render(<DrugListPage />);

    expect(await screen.findByText(/no drugs found/i)).toBeTruthy();
    expect(screen.queryByText(/could not load the drug catalogue/i)).toBeNull();
    // The header and the empty state both offer "Add Drug".
    expect(screen.getAllByRole("button", { name: /add drug/i }).length).toBeGreaterThan(0);
  });

  it("does not show the error state on a successful load", async () => {
    mockDrugList.mockResolvedValue(ONE_DRUG);
    render(<DrugListPage />);

    expect(await screen.findByText("Paracetamol")).toBeTruthy();
    expect(screen.queryByText(/could not load the drug catalogue/i)).toBeNull();
  });
});