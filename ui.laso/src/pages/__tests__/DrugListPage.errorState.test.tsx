/** @vitest-environment jsdom */
/**
 * A failed catalogue load must not be presented as an empty catalogue.
 *
 * Regression: on a non-offline error the page set `error` but still rendered
 * `drugs.length === 0` as "No drugs found.", so a server fault read as a fact
 * about the organization. The empty state is now reserved for a genuinely empty
 * result, and a failed load gets its own state with a Retry action.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockDrugList, mockUser } = vi.hoisted(() => ({
  mockDrugList: vi.fn(),
  mockUser: {
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
  },
}));

vi.mock("@/api/drugs", () => ({
  drugApi: { list: mockDrugList, update: vi.fn() },
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({ user: mockUser, activeBranchId: "branch-1" }),
}));

vi.mock("@/hooks/useCategories", () => ({
  useCategoryTree: () => ({ tree: [], invalidate: vi.fn() }),
}));

vi.mock("@/api/client", () => ({
  isBackendReachable: () => true,
  isBackendKnownUnreachable: () => false,
  isOfflineOrUnreachable: () => false,
  parseApiError: () => "Internal server error: failed to serialize response.",
}));

vi.mock("@/lib/localRead", () => ({ localRead: { searchDrugs: vi.fn() } }));

vi.mock("@/lib/withTimeout", () => ({
  withTimeout: async (primary: () => Promise<unknown>) => {
    // Propagate the rejection so the page's own error handling is exercised.
    return { data: await primary(), isFromCache: false };
  },
}));

vi.mock("@/lib/events", () => ({
  appEvents: { emit: vi.fn() },
  useAppEvent: vi.fn(),
}));

vi.mock("@/components/DataFreshnessIndicator", () => ({
  DataFreshnessIndicator: () => null,
}));
// The page uses named imports, so the factories must expose those names.
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

const EMPTY_PAGE = {
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
  total_pages: 1,
};

describe("DrugListPage error state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows an explicit error state with Retry instead of 'No drugs found' on a 500", async () => {
    mockDrugList.mockRejectedValue(new Error("Internal server error"));

    render(<DrugListPage />);

    await waitFor(() => {
      expect(screen.getByTestId("drug-list-retry")).toBeTruthy();
    });
    expect(screen.getByText(/couldn't load the drug catalogue/i)).toBeTruthy();
    expect(screen.queryByText(/no drugs found/i)).toBeNull();
  });

  it("surfaces the server's message in the error state", async () => {
    mockDrugList.mockRejectedValue(new Error("boom"));

    render(<DrugListPage />);

    await waitFor(() => {
      expect(screen.getByTestId("drug-list-error-detail")).toBeTruthy();
    });
    expect(screen.getByTestId("drug-list-error-detail").textContent).toContain(
      "failed to serialize response"
    );
  });

  it("Retry re-requests and recovers when the second call succeeds", async () => {
    mockDrugList.mockRejectedValueOnce(new Error("boom"));
    mockDrugList.mockResolvedValueOnce({
      items: [
        {
          id: "drug-1",
          name: "Gebedol",
          generic_name: null,
          sku: "GEB-300",
          strength: null,
          category_id: null,
          drug_type: "otc",
          requires_prescription: false,
          unit_price: 5,
          cost_price: null,
          is_active: true,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
    });

    render(<DrugListPage />);

    const retry = await screen.findByTestId("drug-list-retry");
    await act(async () => {
      retry.click();
    });

    await waitFor(() => {
      expect(screen.getByText("Gebedol")).toBeTruthy();
    });
    expect(screen.queryByTestId("drug-list-retry")).toBeNull();
    expect(mockDrugList).toHaveBeenCalledTimes(2);
  });

  it("keeps the empty state for a genuinely empty result", async () => {
    mockDrugList.mockResolvedValue(EMPTY_PAGE);

    render(<DrugListPage />);

    await waitFor(() => {
      expect(screen.getByText(/no drugs found/i)).toBeTruthy();
    });
    expect(screen.queryByTestId("drug-list-retry")).toBeNull();
    expect(screen.queryByText(/couldn't load/i)).toBeNull();
  });
});