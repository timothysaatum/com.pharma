/** @vitest-environment jsdom */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { BranchReconciliationPanel } from "../BranchReconciliationPanel";
import { reconciliationApi } from "@/api/reconciliation";

vi.mock("@/api/reconciliation", () => ({
    reconciliationApi: {
        getReconciliationReport: vi.fn()
    }
}));

describe("BranchReconciliationPanel", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("renders loading state initially", () => {
        vi.mocked(reconciliationApi.getReconciliationReport).mockImplementation(() => new Promise(() => {}));
        render(<BranchReconciliationPanel branchId="b1" />);
        expect(screen.getByText("Loading report...")).toBeDefined();
    });

    it("renders error state", async () => {
        vi.mocked(reconciliationApi.getReconciliationReport).mockRejectedValue(new Error("Network Error"));
        render(<BranchReconciliationPanel branchId="b1" />);
        await waitFor(() => {
            expect(screen.getByText("Network Error")).toBeDefined();
        });
    });

    it("renders summary cards and mismatches", async () => {
        vi.mocked(reconciliationApi.getReconciliationReport).mockResolvedValue({
            branch_id: "b1",
            report_date: "2023-10-10",
            total_drugs_checked: 100,
            balanced_count: 90,
            drift_count: 8,
            dead_letter_count: 2,
            has_drift: true,
            items: [
                {
                    drug_id: "d1",
                    drug_name: "Aspirin",
                    inventory_quantity: 10,
                    batch_sum_quantity: 12,
                    sellable_quantity: 10,
                    unleased_sellable: 0,
                    drift: 2,
                    status: "batch_mismatch"
                }
            ]
        });

        render(<BranchReconciliationPanel branchId="b1" />);

        await waitFor(() => {
            expect(screen.getByText("Daily Reconciliation & Drift Auditor")).toBeDefined();
        });

        // Summary cards
        expect(screen.getByText("100")).toBeDefined(); // total
        expect(screen.getByText("90")).toBeDefined(); // balanced
        expect(screen.getByText("8")).toBeDefined(); // drifted
        expect(screen.getByText("2")).toBeDefined(); // dead letters

        // Table headers and data
        expect(screen.getByText("Aspirin")).toBeDefined();
        expect(screen.getByText("batch_mismatch")).toBeDefined();
        expect(screen.getByText("+2")).toBeDefined();
    });

    it("shows no mismatches text when none exist", async () => {
        vi.mocked(reconciliationApi.getReconciliationReport).mockResolvedValue({
            branch_id: "b1",
            report_date: "2023-10-10",
            total_drugs_checked: 100,
            balanced_count: 100,
            drift_count: 0,
            dead_letter_count: 0,
            has_drift: false,
            items: [
                {
                    drug_id: "d1",
                    drug_name: "Aspirin",
                    inventory_quantity: 10,
                    batch_sum_quantity: 10,
                    sellable_quantity: 10,
                    unleased_sellable: 0,
                    drift: 0,
                    status: "balanced"
                }
            ]
        });

        render(<BranchReconciliationPanel branchId="b1" />);

        await waitFor(() => {
            expect(screen.getByText("No mismatches found.")).toBeDefined();
        });
    });
});
