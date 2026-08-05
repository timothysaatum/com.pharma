/**
 * ===========
 * HTTP wrappers for the sync endpoints.
 * The heavy orchestration logic lives in syncEngine.ts — this is just
 * the network layer.
 *
 * Types are imported from @/types (not from @/lib/syncEngine) so that
 * any module can use them without depending on the engine singleton.
 */

import { get, post } from "@/api/client";
import type {
    PullRequest, PullResponse, PushRequest, PushResponse,
    CrrPushRequest, CrrPushResponse,
    VoidFailedSaleRequest, VoidFailedSaleResponse,
} from "@/types";

export interface SyncStatusResponse {
    server_time: string;
    organization_id: string;
    /** UUID of the authenticated user — returned by the server for diagnostics */
    user_id: string;
}

export const syncApi = {
    /**
     * POST /sync/pull
     * Pull all records changed since last_sync_at for the given branch.
     * Omit last_sync_at on first sync to receive the complete dataset.
     */
    pull: (req: PullRequest): Promise<PullResponse> =>
        post<PullResponse>("/sync/pull", req),

    /**
     * POST /sync/push
     * Push pending branch records to the server for reconciliation.
     */
    push: (req: PushRequest): Promise<PushResponse> =>
        post<PushResponse>("/sync/push", req),

    /**
     * GET /sync/status
     * Returns the current server timestamp for clock calibration.
     * Confirmed GET in the router — not POST.
     */
    status: (): Promise<SyncStatusResponse> =>
        get<SyncStatusResponse>("/sync/status"),

    /** POST /sync/crr-push — push crsql_changes rows for CRR tables */
    crrPush: (req: CrrPushRequest): Promise<CrrPushResponse> =>
        post<CrrPushResponse>("/sync/crr-push", req),

    /** POST /sync/crr-pull — pull crsql_changes delta from server */
    crrPull: (req: PullRequest): Promise<PullResponse> =>
        post<PullResponse>("/sync/crr-pull", req),

    /**
     * POST /sync/void-failed-sale
     * Records an audited, manager-approved decision to stop retrying a
     * dead-lettered offline sale. Requires connectivity — voiding a sale
     * is a compliance-relevant action and must leave a server-side trail,
     * unlike the old purely-local discard.
     */
    voidFailedSale: (req: VoidFailedSaleRequest): Promise<VoidFailedSaleResponse> =>
        post<VoidFailedSaleResponse>("/sync/void-failed-sale", req),
};