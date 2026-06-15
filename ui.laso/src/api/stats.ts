import { get } from "./client";

export interface PaymentMethodSummary {
    count: number;
    amount: number;
}

export interface SalesSummary {
    period: {
        start_date: string;
        end_date: string;
    };
    summary: {
        total_sales: number;
        total_revenue: number;
        average_sale: number;
        total_discount: number;
        total_tax: number;
    };
    payment_methods: Record<string, PaymentMethodSummary>;
}

export interface TopSellingDrug {
    drug_id: string;
    drug_name: string;
    total_quantity: number;
    total_revenue: number;
    sale_count: number;
    average_price: number;
}

export interface BranchStats {
    id: string;
    organization_id: string;
    name: string;
    code: string;
    total_inventory_items: number;
    total_inventory_value: number;
    low_stock_count: number;
    total_sales_today: number;
    total_sales_month: number;
    active_users_count: number;
}

export const statsApi = {
    /**
     * GET /stats/{branch_id}
     * Fetches comprehensive branch statistics.
     */
    getBranchStats(branchId: string, signal?: AbortSignal): Promise<BranchStats> {
        return get<BranchStats>(`/stats/${branchId}`, { signal });
    },

    /**
     * GET /stats/reports/summary
     * Fetches sales summary data for the requested date range.
     */
    getSalesSummary(
        startDate: string,
        endDate: string,
        branchId?: string,
        signal?: AbortSignal
    ): Promise<SalesSummary> {
        const params: Record<string, unknown> = {
            start_date: startDate,
            end_date: endDate,
        };
        if (branchId) params.branch_id = branchId;

        return get<SalesSummary>(`/stats/reports/summary`, {
            params,
            signal,
        });
    },

    /**
     * GET /stats/reports/top-selling
     * Fetches top-selling drugs for the requested date range.
     */
    getTopSelling(
        startDate: string,
        endDate: string,
        branchId?: string,
        limit = 10,
        signal?: AbortSignal
    ): Promise<TopSellingDrug[]> {
        const params: Record<string, unknown> = {
            start_date: startDate,
            end_date: endDate,
            limit,
        };
        if (branchId) params.branch_id = branchId;

        return get<TopSellingDrug[]>(`/stats/reports/top-selling`, {
            params,
            signal,
        });
    },
};
