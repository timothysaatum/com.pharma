import { get } from "./client";
import type { PaginatedResponse } from "@/types";

export interface AuditLogEntry {
    id: string;
    organization_id: string;
    user_id?: string | null;
    user_full_name?: string | null;
    action: string;
    entity_type?: string | null;
    entity_id?: string | null;
    changes?: Record<string, any> | null;
    ip_address?: string | null;
    user_agent?: string | null;
    context_metadata?: Record<string, any> | null;
    created_at: string;
}

export const auditApi = {
    list(params: {
        page?: number;
        page_size?: number;
        user_id?: string;
        action?: string;
        entity_type?: string;
    } = {}): Promise<PaginatedResponse<AuditLogEntry>> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
        });
        return get<PaginatedResponse<AuditLogEntry>>(`/audit/?${qs}`);
    }
};
