/** @vitest-environment jsdom */
import { render, screen } from "@testing-library/react";
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
  drugApi: {
    list: mockDrugList,
    update: vi.fn(),
  },
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: mockUser,
    activeBranchId: "branch-1",
  }),
}));

vi.mock("@/hooks/useCategories", () => ({
  useCategoryTree: () => ({ tree: [], invalidate: vi.fn() }),
}));

vi.mock("@/api/client", () => ({
  isBackendReachable: () => true,
  parseApiError: () => "Request failed",
}));

vi.mock("@/lib/localRead", () => ({
  localRead: {
    searchDrugs: vi.fn(),
  },
}));

vi.mock("@/lib/localDb", () => ({
  cacheBranchScopedDrugs: vi.fn(),
}));

vi.mock("@/lib/withTimeout", () => ({
  withTimeout: async (primary: () => Promise<unknown>) => ({
    data: await primary(),
    isFromCache: false,
  }),
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

describe("DrugListPage permissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockDrugList.mockResolvedValue({
      items: [
        {
          id: "drug-1",
          name: "Paracetamol",
          generic_name: "Acetaminophen",
          sku: "PAR-500",
          strength: "500mg",
          category_id: null,
          drug_type: "otc",
          requires_prescription: false,
          unit_price: 10,
          cost_price: 6,
          is_active: true,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
    });
  });

  it("shows Add Drug for branch-assigned users with manage_drugs permission", async () => {
    render(<DrugListPage />);

    expect(await screen.findByRole("button", { name: /add drug/i })).toBeTruthy();
  });
});
