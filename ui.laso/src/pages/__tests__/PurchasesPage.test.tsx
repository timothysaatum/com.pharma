/** @vitest-environment jsdom */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/stores/authStore", () => ({
    useAuthStore: vi.fn(),
}));

vi.mock("@/hooks/usePurchaseOrders", () => ({
    usePurchaseOrders: vi.fn(),
}));

vi.mock("@/hooks/usePurchaseOrderDetail", () => ({
    usePurchaseOrderDetail: () => ({
        loading: false,
        error: null,
        po: null,
        mutating: false,
        mutateError: null,
        refresh: vi.fn(),
        receiveGoods: vi.fn(),
    }),
}));

vi.mock("@/api/purchases", () => ({
    suppliersApi: { create: vi.fn() },
}));

vi.mock("@/components/purchases/CreatePOModal", () => ({
    CreatePOModal: () => null,
}));

vi.mock("@/components/purchases/PurchaseOrderDetailPanel", () => ({
    PurchaseOrderDetailPanel: () => null,
}));

import { usePurchaseOrders } from "@/hooks/usePurchaseOrders";
import { useAuthStore } from "@/stores/authStore";
import PurchasesPage from "../PurchasesPage";


function emptyPurchaseOrderHook() {
    return {
        orders: [],
        suppliers: [],
        suppliersError: null,
        total: 0,
        page: 1,
        totalPages: 1,
        statusFilter: "",
        supplierFilter: "",
        listLoading: false,
        listError: null,
        actionState: { loading: false, error: null },
        creating: false,
        createError: null,
        setStatusFilter: vi.fn(),
        setSupplierFilter: vi.fn(),
        refresh: vi.fn(),
        refreshSuppliers: vi.fn(),
        appendSupplier: vi.fn(),
        goToPage: vi.fn(),
        createOrder: vi.fn(),
        submitOrder: vi.fn(),
        approveOrder: vi.fn(),
        rejectOrder: vi.fn(),
        cancelOrder: vi.fn(),
    };
}


function user(assignedBranches: string[], permissions: string[] = []) {
    return {
        id: "user-1",
        organization_id: "org-1",
        full_name: "Test User",
        is_super_admin: false,
        assigned_branches: assignedBranches,
        roles: permissions.length > 0
            ? [{ id: "role-1", name: "Test Role", permissions }]
            : [],
    };
}


describe("PurchasesPage branch assignment empty states", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.mocked(usePurchaseOrders).mockReturnValue(emptyPurchaseOrderHook() as never);
    });

    it("explains that a user with no assigned branches must contact an administrator", () => {
        vi.mocked(useAuthStore).mockReturnValue({
            user: user([]),
            activeBranchId: null,
        } as never);

        render(<PurchasesPage />);

        expect(screen.getByText("No branches assigned — contact your administrator")).toBeTruthy();
        expect(screen.queryByText("Create your first order using the button above")).toBeNull();
    });

    it("requests the assigned-branch aggregate when a multi-branch user has no active selection", () => {
        vi.mocked(useAuthStore).mockReturnValue({
            user: user(["branch-1", "branch-2"]),
            activeBranchId: null,
        } as never);

        render(<PurchasesPage />);

        expect(usePurchaseOrders).toHaveBeenCalledWith({ branch_id: undefined });
        expect(screen.getByText("No purchase orders")).toBeTruthy();
        expect(screen.queryByText("No branches assigned — contact your administrator")).toBeNull();
    });

    it("does not show the assignment warning to an elevated user with organization-wide access", () => {
        vi.mocked(useAuthStore).mockReturnValue({
            user: user([], ["view_reports"]),
            activeBranchId: null,
        } as never);

        render(<PurchasesPage />);

        expect(screen.getByText("No purchase orders")).toBeTruthy();
        expect(screen.queryByText("No branches assigned — contact your administrator")).toBeNull();
    });
});
