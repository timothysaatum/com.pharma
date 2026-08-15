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
    VoidFailedSaleRequest, VoidFailedSaleResponse,
} from "@/types";
import type {
    EventPushRequest,
    EventPushResponse,
    EventPullResponse,
} from "@/lib/eventEnvelope";

export interface SyncStatusResponse {
    server_time: string;
    organization_id: string;
    /** UUID of the authenticated user — returned by the server for diagnostics */
    user_id: string;
}

export const syncApi = {
    /**
     * GET /sync/status
     * Returns the current server timestamp for clock calibration.
     * Confirmed GET in the router — not POST.
     */
    status: (): Promise<SyncStatusResponse> =>
        get<SyncStatusResponse>("/sync/status"),

    /**
     * POST /sync/void-failed-sale
     * Records an audited, manager-approved decision to stop retrying a
     * dead-lettered offline sale. Requires connectivity — voiding a sale
     * is a compliance-relevant action and must leave a server-side trail,
     * unlike the old purely-local discard.
     */
    voidFailedSale: (req: VoidFailedSaleRequest): Promise<VoidFailedSaleResponse> =>
        post<VoidFailedSaleResponse>("/sync/void-failed-sale", req),

    /** POST /sync/events — push a batch of client-authored event envelopes */
    pushEvents: (req: EventPushRequest): Promise<EventPushResponse> =>
        post<EventPushResponse>("/sync/events", req),

    /** GET /sync/events?after_seq=N&limit=N — pull events since a seq cursor */
    pullEvents: (afterSeq: number, limit = 200): Promise<EventPullResponse> =>
        get<EventPullResponse>(`/sync/events?after_seq=${afterSeq}&limit=${limit}`),
};