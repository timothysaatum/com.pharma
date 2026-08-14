/** @vitest-environment jsdom */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { inventoryApi } from "@/api/inventory";
import { drugApi } from "@/api/drugs";
import { branchApi } from "@/api/branches";
import { writeLocal } from "@/lib/localWrite";
import { localRead } from "@/lib/localRead";
import InventoryPage from "@/pages/InventoryPage";
import type { BranchInventoryWithDetails, Drug, BranchListItem } from "@/types";

// ─── Fixtures ───────────────────────────────────────────────────────────────

const inventoryItem: BranchInventoryWithDetails = {
    id: "bi-1",
    branch_id: "branch-1",
    drug_id: "drug-1",
    quantity: 20,
    reserved_quantity: 0,
    location: "Aisle 1",
    selling_price: null,
    sync_status: "synced",
    sync_version: 1,
    synced_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    drug_name: "Paracetamol 500mg",
    drug_sku: "PARA-500",
    drug_unit_price: 5,
    effective_unit_price: 5,
    drug_reorder_level: 10,
    branch_name: "Main Branch",
    branch_code: "MB",
    available_quantity: 20,
};

const catalogueDrug: Drug = {
    id: "drug-2",
    organization_id: "org-abc",
    name: "Ibuprofen 200mg",
    generic_name: null,
    sku: "IBU-200",
    barcode: null,
    drug_type: "otc",
    dosage_form: "tablet",
    strength: "200mg",
    unit_price: 3,
    cost_price: 1.5,
    reorder_level: 5,
    requires_prescription: false,
    supplier: null,
    is_active: true,
    sync_status: "synced",
    sync_version: 1,
    synced_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
} as unknown as Drug;

const otherBranch: BranchListItem = {
    id: "branch-2",
    organization_id: "org-abc",
    name: "Second Branch",
    code: "SB",
    is_active: true,
    manager_id: null,
    manager_name: null,
    phone: null,
    email: null,
    created_at: "2026-01-01T00:00:00Z",
} as unknown as BranchListItem;

const emptyPage = <T,>(items: T[] = []) => ({
    items,
    total: items.length,
    page: 1,
    page_size: 25,
    total_pages: 1,
    has_next: false,
    has_prev: false,
});

const lowStockReport = { items: [], total_items: 0, out_of_stock_count: 0, low_stock_count: 0 };
const expiringReport = { items: [], total_items: 0, total_quantity: 0, total_cost_value: 0, total_selling_value: 0 };

// ─── Mocks ──────────────────────────────────────────────────────────────────

vi.mock("@/stores/authStore", () => ({
    useAuthStore: () => ({
        activeBranchId: "branch-1",
        user: { id: "user-1", full_name: "Jane Pharmacist", organization_id: "org-abc" },
    }),
}));

vi.mock("@/api/inventory", () => ({
    inventoryApi: {
        getBranchInventory: vi.fn(),
        getLowStock: vi.fn(),
        getExpiring: vi.fn(),
        getValuation: vi.fn(),
        getBatches: vi.fn(),
        adjust: vi.fn(),
        updateBranchDrug: vi.fn(),
        addDrugToBranch: vi.fn(),
        removeBranchDrug: vi.fn(),
        transfer: vi.fn(),
        createBatch: vi.fn(),
    },
}));

vi.mock("@/api/drugs", () => ({
    drugApi: {
        list: vi.fn(),
        getById: vi.fn(),
    },
}));

vi.mock("@/api/branches", () => ({
    branchApi: {
        list: vi.fn(),
    },
}));

vi.mock("@/lib/localRead", () => ({
    localRead: {
        getBranchInventory: vi.fn(),
        getLowStock: vi.fn(),
        getExpiring: vi.fn(),
        getValuation: vi.fn(),
        getBatchesForDrug: vi.fn(),
    },
}));

vi.mock("@/lib/localWrite", () => ({
    writeLocal: {
        stockAdjustment: vi.fn().mockResolvedValue(undefined),
        branchDrug: vi.fn().mockResolvedValue(undefined),
        inventory: vi.fn().mockResolvedValue(undefined),
    },
}));

vi.mock("@/lib/localDb", () => ({
    cacheBranchInventoryRows: vi.fn().mockResolvedValue(undefined),
}));

const isOfflineErrorMock = vi.fn((_err: unknown) => true);

vi.mock("@/api/client", () => ({
    isBackendReachable: () => true,
    isOfflineError: (err: unknown) => isOfflineErrorMock(err),
    parseApiError: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

vi.mock("@/lib/events", () => ({
    appEvents: { emit: vi.fn() },
    useAppEvent: vi.fn(),
}));

vi.mock("framer-motion", () => ({
    AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
    motion: new Proxy(
        {},
        {
            get: () => {
                const Tag = ({ children, ...rest }: { children?: ReactNode; [k: string]: unknown }) => {
                    const cleanProps = Object.fromEntries(
                        Object.entries(rest).filter(([k]) =>
                            !["initial", "animate", "exit", "transition", "whileHover", "whileTap", "layout"].includes(k)
                        )
                    );
                    return <div {...cleanProps}>{children}</div>;
                };
                return Tag;
            },
        }
    ),
}));

class OfflineError extends Error {
    constructor() {
        super("Network unavailable");
        this.name = "OfflineError";
    }
}

describe("InventoryPage offline write handling", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        isOfflineErrorMock.mockReturnValue(true);
        Object.defineProperty(navigator, "onLine", { value: true, configurable: true, writable: true });

        vi.mocked(inventoryApi.getBranchInventory).mockResolvedValue(emptyPage([inventoryItem]));
        vi.mocked(inventoryApi.getLowStock).mockResolvedValue(lowStockReport as never);
        vi.mocked(inventoryApi.getExpiring).mockResolvedValue(expiringReport as never);
        vi.mocked(localRead.getBranchInventory).mockResolvedValue(emptyPage([inventoryItem]));
        vi.mocked(localRead.getLowStock).mockResolvedValue(lowStockReport as never);
        vi.mocked(localRead.getExpiring).mockResolvedValue(expiringReport as never);
        vi.mocked(branchApi.list).mockResolvedValue(emptyPage([otherBranch]) as never);
        vi.mocked(drugApi.list).mockResolvedValue(emptyPage([catalogueDrug]) as never);
    });

    afterEach(() => {
        Object.defineProperty(navigator, "onLine", { value: true, configurable: true, writable: true });
    });

    it("falls back to writeLocal.stockAdjustment with the correct payload when inventoryApi.adjust is offline", async () => {
        vi.mocked(inventoryApi.adjust).mockRejectedValue(new OfflineError());

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByTitle("Adjust stock"));
        await screen.findByText("Adjust Stock");

        fireEvent.change(screen.getByPlaceholderText("Enter quantity"), { target: { value: "5" } });
        fireEvent.click(screen.getByRole("button", { name: /record adjustment/i }));

        await waitFor(() => expect(writeLocal.stockAdjustment).toHaveBeenCalledTimes(1));
        const payload = vi.mocked(writeLocal.stockAdjustment).mock.calls[0][0];
        expect(payload).toMatchObject({
            branch_id: "branch-1",
            drug_id: "drug-1",
            adjustment_type: "damage",
            quantity_change: -5,
            adjusted_by: "user-1",
        });
        expect(typeof payload.id).toBe("string");
        expect(payload.id.length).toBeGreaterThan(0);
    });

    it("surfaces the real error and does not queue a write when inventoryApi.adjust fails for a non-offline reason", async () => {
        isOfflineErrorMock.mockReturnValue(false);
        vi.mocked(inventoryApi.adjust).mockRejectedValue(new Error("permission denied"));

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByTitle("Adjust stock"));
        await screen.findByText("Adjust Stock");
        fireEvent.change(screen.getByPlaceholderText("Enter quantity"), { target: { value: "5" } });
        fireEvent.click(screen.getByRole("button", { name: /record adjustment/i }));

        await screen.findByText("permission denied");
        expect(writeLocal.stockAdjustment).not.toHaveBeenCalled();
    });

    it("falls back to writeLocal.branchDrug when adding a drug to the branch offline", async () => {
        vi.mocked(inventoryApi.addDrugToBranch).mockRejectedValue(new OfflineError());

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByRole("button", { name: /add drugs/i }));
        await screen.findByText("Ibuprofen 200mg");
        fireEvent.click(screen.getByText("Ibuprofen 200mg"));
        fireEvent.click(screen.getByRole("button", { name: /add to branch/i }));

        await waitFor(() => expect(writeLocal.branchDrug).toHaveBeenCalledTimes(1));
        const [payload, operation] = vi.mocked(writeLocal.branchDrug).mock.calls[0];
        expect(operation).toBe("create");
        expect(payload).toMatchObject({
            branch_id: "branch-1",
            drug_id: "drug-2",
            quantity: 0,
            reserved_quantity: 0,
        });
        expect(typeof payload.id).toBe("string");
    });

    it("falls back to writeLocal.branchDrug when updating branch drug settings offline", async () => {
        vi.mocked(inventoryApi.updateBranchDrug).mockRejectedValue(new OfflineError());

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByTitle("Branch settings"));
        await screen.findByText("Branch Settings");

        const locationInput = screen.getByPlaceholderText("e.g. Aisle 2 / Shelf B");
        fireEvent.change(locationInput, { target: { value: "Aisle 9" } });
        fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

        await waitFor(() => expect(writeLocal.branchDrug).toHaveBeenCalledTimes(1));
        const [payload, operation] = vi.mocked(writeLocal.branchDrug).mock.calls[0];
        expect(operation).toBe("update");
        expect(payload).toMatchObject({
            id: "bi-1",
            branch_id: "branch-1",
            drug_id: "drug-1",
            location: "Aisle 9",
        });
    });

    it("disables the Remove button in Branch Settings while offline", async () => {
        Object.defineProperty(navigator, "onLine", { value: false, configurable: true, writable: true });

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByTitle("Branch settings"));
        await screen.findByText("Branch Settings");

        const removeButton = screen.getByRole("button", { name: /remove/i }) as HTMLButtonElement;
        expect(removeButton.disabled).toBe(true);
        expect(removeButton.getAttribute("title")).toBe(
            "Removing a drug from a branch requires an online connection"
        );
        expect(inventoryApi.removeBranchDrug).not.toHaveBeenCalled();
    });

    it("disables the Transfer Stock submit button while offline even with a valid form", async () => {
        Object.defineProperty(navigator, "onLine", { value: false, configurable: true, writable: true });

        render(<InventoryPage />);
        await screen.findByText("Paracetamol 500mg");

        fireEvent.click(screen.getByTitle("Transfer to another branch"));
        await screen.findByText("Transfer Stock", { selector: "h2" });

        // Fill in every other required field so the only reason the button
        // could still be disabled is the new offline check.
        await screen.findByText("Second Branch (SB)");
        fireEvent.change(screen.getByRole("combobox"), { target: { value: "branch-2" } });
        fireEvent.change(screen.getByPlaceholderText("Enter quantity"), { target: { value: "5" } });
        fireEvent.change(
            screen.getByPlaceholderText("e.g. Restocking branch due to demand surge"),
            { target: { value: "Restocking due to demand" } }
        );

        const submitButton = screen.getByRole("button", { name: /transfer stock/i }) as HTMLButtonElement;
        expect(submitButton.disabled).toBe(true);

        fireEvent.click(submitButton);
        expect(inventoryApi.transfer).not.toHaveBeenCalled();
    });
});
