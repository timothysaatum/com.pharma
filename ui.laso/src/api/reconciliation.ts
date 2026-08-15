import { get } from "./client";
import type { ReconciliationReportResponse } from "@/types";

const BASE = "/inventory/branch";

export const reconciliationApi = {
    /**
     * GET /inventory/branch/{branch_id}/reconciliation/report
     * Daily reconciliation report for a branch.
     */
    getReconciliationReport(branchId: string, signal?: AbortSignal): Promise<ReconciliationReportResponse> {
        return get<ReconciliationReportResponse>(
            `${BASE}/${branchId}/reconciliation/report`,
            { signal }
        );
    }
};
