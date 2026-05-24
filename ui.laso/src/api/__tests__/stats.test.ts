import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

vi.mock("../client", () => ({
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
}));

import { statsApi } from "../stats";
import { get as mockedGet } from "../client";

const typedGet = mockedGet as unknown as Mock<any, any>;

describe("statsApi", () => {
    beforeEach(() => {
        typedGet.mockReset();
    });

    it("should call the sales summary endpoint with branchId when provided", async () => {
        const payload = {
            period: { start_date: "2024-01-01", end_date: "2024-01-31" },
            summary: {
                total_sales: 10,
                total_revenue: 1000,
                average_sale: 100,
                total_discount: 50,
                total_tax: 25,
            },
            payment_methods: {
                cash: { count: 5, amount: 500 },
            },
        };
        typedGet.mockResolvedValueOnce(payload);

        const result = await statsApi.getSalesSummary("2024-01-01", "2024-01-31", "branch-1");

        expect(typedGet).toHaveBeenCalledTimes(1);
        expect(typedGet).toHaveBeenCalledWith("/stats/reports/summary", {
            params: {
                start_date: "2024-01-01",
                end_date: "2024-01-31",
                branch_id: "branch-1",
            },
            signal: undefined,
        });
        expect(result).toBe(payload);
    });

    it("should call the top-selling endpoint with default limit and without branchId", async () => {
        const payload = [
            {
                drug_id: "drug-1",
                drug_name: "Aspirin",
                total_quantity: 50,
                total_revenue: 500,
                sale_count: 20,
                average_price: 10,
            },
        ];
        typedGet.mockResolvedValueOnce(payload);

        const result = await statsApi.getTopSelling("2024-02-01", "2024-02-28");

        expect(typedGet).toHaveBeenCalledTimes(1);
        expect(typedGet).toHaveBeenCalledWith("/stats/reports/top-selling", {
            params: {
                start_date: "2024-02-01",
                end_date: "2024-02-28",
                limit: 10,
            },
            signal: undefined,
        });
        expect(result).toBe(payload);
    });
});
