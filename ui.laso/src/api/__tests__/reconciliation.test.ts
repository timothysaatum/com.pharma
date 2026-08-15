import { describe, it, expect, vi, beforeEach } from "vitest";
import { reconciliationApi } from "../reconciliation";
import { get } from "../client";

vi.mock("../client", () => ({
    get: vi.fn(),
}));

describe("reconciliationApi", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("should call GET with correct url for getReconciliationReport", async () => {
        const mockResponse = {
            branch_id: "branch-1",
            report_date: "2023-10-10",
            total_drugs_checked: 10,
            balanced_count: 5,
            drift_count: 3,
            dead_letter_count: 2,
            items: [],
            has_drift: true,
        };

        vi.mocked(get).mockResolvedValue(mockResponse);

        const response = await reconciliationApi.getReconciliationReport("branch-1");

        expect(get).toHaveBeenCalledWith(
            "/inventory/branch/branch-1/reconciliation/report",
            { signal: undefined }
        );
        expect(response).toEqual(mockResponse);
    });
});
