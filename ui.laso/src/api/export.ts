import { apiClient } from "./client";

export const exportApi = {
    /**
     * GET /export/sales/excel?year=&month=&branch_id=&contract_id=&contract_type=
     * Returns an XLSX file as ArrayBuffer
     */
    exportSalesExcel: async (params: {
        year: number;
        month?: number | null;
        branch_id?: string | null;
        contract_id?: string | null;
        contract_type?: string | null;
    }): Promise<ArrayBuffer> => {
        const qs = new URLSearchParams();
        qs.set("year", String(params.year));
        if (params.month) qs.set("month", String(params.month));
        if (params.branch_id) qs.set("branch_id", params.branch_id);
        if (params.contract_id) qs.set("contract_id", params.contract_id);
        if (params.contract_type) qs.set("contract_type", params.contract_type);

        const resp = await apiClient.get(`/export/sales/excel?${qs.toString()}`, {
            responseType: "arraybuffer",
        });
        return resp.data as ArrayBuffer;
    },

    /**
     * GET /export/inventory/excel?year=&month=&branch_id=
     */
    exportInventoryExcel: async (params: {
        year: number;
        month?: number | null;
        branch_id?: string | null;
    }): Promise<ArrayBuffer> => {
        const qs = new URLSearchParams();
        qs.set("year", String(params.year));
        if (params.month) qs.set("month", String(params.month));
        if (params.branch_id) qs.set("branch_id", params.branch_id);

        const resp = await apiClient.get(`/export/inventory/excel?${qs.toString()}`, {
            responseType: "arraybuffer",
        });
        return resp.data as ArrayBuffer;
    },
};
