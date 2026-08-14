/**
 * API wrappers for the /conflicts endpoints.
 *
 * Endpoints:
 *   GET  /conflicts              list conflicts for the caller's org
 *   POST /conflicts/{id}/resolve resolve a conflict (keep_local | keep_incoming)
 */

import { get, post } from "./client";
import type { VectorClock } from "@/lib/localDb";

export interface ConflictRecord {
  id: string;
  org_id: string;
  aggregate_type: string;
  aggregate_id: string;
  event_id: string | null;
  local_vector: VectorClock;
  local_snapshot: Record<string, unknown>;
  incoming_vector: VectorClock;
  incoming_payload: Record<string, unknown>;
  status: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
}

export interface ConflictListResponse {
  conflicts: ConflictRecord[];
  total: number;
  pending: number;
}

export type ConflictResolution = "keep_local" | "keep_incoming";

export const conflictsApi = {
  list: (params?: {
    status?: "pending" | "resolved_keep_local" | "resolved_keep_incoming" | "all";
    aggregate_type?: string;
    page?: number;
    page_size?: number;
  }): Promise<ConflictListResponse> => {
    const qs = new URLSearchParams();
    if (params?.status) qs.set("status", params.status);
    if (params?.aggregate_type) qs.set("aggregate_type", params.aggregate_type);
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    const query = qs.toString();
    return get(`/conflicts${query ? `?${query}` : ""}`);
  },

  resolve: (
    conflictId: string,
    resolution: ConflictResolution,
  ): Promise<ConflictRecord> =>
    post(`/conflicts/${conflictId}/resolve`, { resolution }),
};
