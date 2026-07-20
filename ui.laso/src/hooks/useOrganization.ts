/**
 * useOrganization.ts
 * ─────────────────────────────────────────────────────────────
 * Manages all async state for the Organization settings tab.
 *
 *  - Loads the current user's org + stats on mount
 *  - Exposes updateOrg() for name/phone/email/address (PATCH /organizations/{id})
 *  - Exposes updateSettings() for operational settings  (PATCH /organizations/{id}/settings)
 *
 * Both mutations optimistically update local state and fall back to a
 * server re-fetch on error — same pattern used throughout the app.
 */

import { useState, useEffect, useCallback } from "react";
import { organizationApi, type OrganizationSettingsUpdate } from "@/api/organization";
import { parseApiError, isOfflineError, isBackendReachable, isBackendKnownUnreachable } from "@/api/client";
import { offlineCache } from "@/lib/storage";
import { useAuthStore } from "@/stores/authStore";
import type { Organization, OrganizationStats, OrganizationUpdate } from "@/types";

interface MutationState {
    loading: boolean;
    error: string | null;
    success: boolean;
}

const IDLE: MutationState = { loading: false, error: null, success: false };

export function useOrganization() {
    const { user } = useAuthStore();
    const orgId = user?.organization_id ?? null;

    const [org, setOrg] = useState<Organization | null>(null);
    const [stats, setStats] = useState<OrganizationStats | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [orgMutation, setOrgMutation] = useState<MutationState>(IDLE);
    const [settingsMutation, setSettingsMutation] = useState<MutationState>(IDLE);
    const [isOffline, setIsOffline] = useState(false);

    // ── Load org + stats in parallel with offline cache fallback ─────────
    const load = useCallback(async () => {
        if (!orgId) return;
        setLoading(true);
        setError(null);
        setIsOffline(false);

        if (isBackendKnownUnreachable()) {
            const [cachedOrg, cachedStats] = await Promise.all([
                offlineCache.getOrganization({ allowExpired: true }),
                offlineCache.getOrganizationStats({ allowExpired: true }),
            ]);
            if (cachedOrg) {
                setOrg(cachedOrg);
                setStats(cachedStats);
                setIsOffline(true);
            } else {
                setError("Offline mode: no cached organisation data available.");
            }
            setLoading(false);
            return;
        }

        try {
            const [orgData, statsData] = await Promise.all([
                organizationApi.getById(orgId),
                organizationApi.getStats(orgId),
            ]);
            setOrg(orgData);
            setStats(statsData);
            await Promise.all([
                offlineCache.setOrganization(orgData),
                offlineCache.setOrganizationStats(statsData),
            ]);
        } catch (err) {
            if (isOfflineError(err)) {
                const [cachedOrg, cachedStats] = await Promise.all([
                    offlineCache.getOrganization({ allowExpired: true }),
                    offlineCache.getOrganizationStats({ allowExpired: true }),
                ]);
                if (cachedOrg) {
                    setOrg(cachedOrg);
                    setStats(cachedStats);
                    setIsOffline(true);
                } else {
                    setError(parseApiError(err));
                }
            } else {
                setError(parseApiError(err));
            }
        } finally {
            setLoading(false);
        }
    }, [orgId]);

    useEffect(() => {
        load();
    }, [load]);

    // ── Update org profile (name, phone, email, address) ─────
    const updateOrg = useCallback(
        async (data: OrganizationUpdate): Promise<boolean> => {
            if (!orgId) return false;
            if (!isBackendReachable()) {
                setOrgMutation({
                    loading: false,
                    error: "Cannot save organisation data while offline. Reconnect and try again.",
                    success: false,
                });
                return false;
            }
            setOrgMutation({ loading: true, error: null, success: false });
            try {
                const updated = await organizationApi.update(orgId, data);
                setOrg(updated);
                await offlineCache.setOrganization(updated);
                setOrgMutation({ loading: false, error: null, success: true });
                setTimeout(() => setOrgMutation(IDLE), 3000);
                return true;
            } catch (err) {
                const message = isOfflineError(err)
                    ? "Cannot save organisation data while offline. Reconnect and try again."
                    : parseApiError(err);
                setOrgMutation({ loading: false, error: message, success: false });
                return false;
            }
        },
        [orgId],
    );

    // ── Update org settings (operational preferences) ────────
    const updateSettings = useCallback(
        async (data: OrganizationSettingsUpdate): Promise<boolean> => {
            if (!orgId) return false;
            if (!isBackendReachable()) {
                setSettingsMutation({
                    loading: false,
                    error: "Cannot save organisation settings while offline. Reconnect and try again.",
                    success: false,
                });
                return false;
            }
            setSettingsMutation({ loading: true, error: null, success: false });
            try {
                const updated = await organizationApi.updateSettings(orgId, data);
                setOrg(updated);
                await offlineCache.setOrganization(updated);
                setSettingsMutation({ loading: false, error: null, success: true });
                setTimeout(() => setSettingsMutation(IDLE), 3000);
                return true;
            } catch (err) {
                const message = isOfflineError(err)
                    ? "Cannot save organisation settings while offline. Reconnect and try again."
                    : parseApiError(err);
                setSettingsMutation({ loading: false, error: message, success: false });
                return false;
            }
        },
        [orgId],
    );

    return {
        org,
        stats,
        loading,
        error,
        isOffline,
        orgMutation,
        settingsMutation,
        updateOrg,
        updateSettings,
        refresh: load,
    };
}
