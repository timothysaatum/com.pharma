/**
 * useBranches.ts
 * ─────────────────────────────────────────────────────────────
 * All async state for the Branches settings tab:
 *  - Full branch list for the org
 *  - Create, activate, deactivate
 *  - Optimistic status toggles
 */

import { useState, useEffect, useCallback } from "react";
import { branchApi } from "@/api/branches";
import { parseApiError, isOfflineError, isBackendReachable } from "@/api/client";
import { offlineCache } from "@/lib/storage";
import { useAuthStore } from "@/stores/authStore";
import type { BranchListItem, BranchCreate } from "@/types";

interface ActionState {
    loading: boolean;
    error: string | null;
}

export function useBranches() {
    const { user } = useAuthStore();

    const [branches, setBranches] = useState<BranchListItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);
    const [isOffline, setIsOffline] = useState(false);

    const [actionState, setActionState] = useState<ActionState>({ loading: false, error: null });

    // ── Fetch all branches for the org with offline cache fallback ─────
    const fetchBranches = useCallback(async () => {
        setLoading(true);
        setError(null);
        setIsOffline(false);

        if (!isBackendReachable()) {
            const cached = await offlineCache.getBranches();
            if (cached) {
                setBranches(cached);
                setIsOffline(true);
            } else {
                setError("Cannot load branches while offline and no cached data is available.");
            }
            setLoading(false);
            return;
        }

        try {
            const result = await branchApi.list({ page_size: 200 });
            setBranches(result.items);
            await offlineCache.setBranches(result.items);
        } catch (err) {
            if (isOfflineError(err)) {
                const cached = await offlineCache.getBranches();
                if (cached) {
                    setBranches(cached);
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
    }, []);

    useEffect(() => {
        fetchBranches();
    }, [fetchBranches]);

    // ── Create ────────────────────────────────────────────────
    const createBranch = useCallback(
        async (data: BranchCreate): Promise<boolean> => {
            if (!isBackendReachable()) {
                setCreateError("Cannot create a branch while offline. Reconnect and try again.");
                return false;
            }
            setCreating(true);
            setCreateError(null);
            try {
                const branch = await branchApi.create({
                    ...data,
                    organization_id: user?.organization_id,
                });
                const listItem: BranchListItem = {
                    id: branch.id,
                    organization_id: branch.organization_id,
                    name: branch.name,
                    code: branch.code,
                    is_active: branch.is_active,
                    manager_id: branch.manager_id,
                    manager_name: branch.manager_name,
                    phone: branch.phone,
                    email: branch.email,
                    created_at: branch.created_at,
                };
                setBranches((prev) =>
                    [...prev, listItem].sort((a, b) => a.name.localeCompare(b.name))
                );
                await offlineCache.setBranches(
                    [...branches, listItem].sort((a, b) => a.name.localeCompare(b.name))
                );
                return true;
            } catch (err) {
                const message = isOfflineError(err)
                    ? "Cannot create a branch while offline. Reconnect and try again."
                    : parseApiError(err);
                setCreateError(message);
                return false;
            } finally {
                setCreating(false);
            }
        },
        [user?.organization_id, branches],
    );

    // ── Activate / Deactivate ─────────────────────────────────
    const runAction = useCallback(async (fn: () => Promise<void>): Promise<boolean> => {
        setActionState({ loading: true, error: null });
        try {
            await fn();
            setActionState({ loading: false, error: null });
            return true;
        } catch (err) {
            setActionState({ loading: false, error: parseApiError(err) });
            return false;
        }
    }, []);

    const activateBranch = useCallback(
        async (id: string) => {
            if (!isBackendReachable()) {
                setActionState({
                    loading: false,
                    error: "Cannot change branch status while offline. Reconnect and try again.",
                });
                return false;
            }
            setBranches((prev) =>
                prev.map((b) => (b.id === id ? { ...b, is_active: true } : b))
            );
            const ok = await runAction(async () => {
                await branchApi.activate(id);
            });
            if (!ok) await fetchBranches();
            return ok;
        },
        [runAction, fetchBranches],
    );

    const deactivateBranch = useCallback(
        async (id: string) => {
            if (!isBackendReachable()) {
                setActionState({
                    loading: false,
                    error: "Cannot change branch status while offline. Reconnect and try again.",
                });
                return false;
            }
            setBranches((prev) =>
                prev.map((b) => (b.id === id ? { ...b, is_active: false } : b))
            );
            const ok = await runAction(async () => {
                await branchApi.deactivate(id);
            });
            if (!ok) await fetchBranches();
            return ok;
        },
        [runAction, fetchBranches],
    );

    return {
        branches,
        setBranches,   // exposed so BranchesTab can merge edits without refetch
        loading,
        error,
        isOffline,
        creating,
        createError,
        clearCreateError: () => setCreateError(null),
        actionState,
        createBranch,
        activateBranch,
        deactivateBranch,
        refresh: fetchBranches,
    };
}