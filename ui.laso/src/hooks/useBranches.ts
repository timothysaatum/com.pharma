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
import { parseApiError } from "@/api/client";
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

    const [actionState, setActionState] = useState<ActionState>({ loading: false, error: null });

    // ── Fetch all branches for the org ────────────────────────
    const fetchBranches = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const result = await branchApi.list({ page_size: 200 });
            setBranches(result.items);
        } catch (err) {
            setError(parseApiError(err));
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
            setCreating(true);
            setCreateError(null);
            try {
                const branch = await branchApi.create({
                    ...data,
                    organization_id: user?.organization_id,
                });
                // Optimistically append as BranchListItem
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
                return true;
            } catch (err) {
                setCreateError(parseApiError(err));
                return false;
            } finally {
                setCreating(false);
            }
        },
        [user?.organization_id],
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
            // Optimistic update
            setBranches((prev) =>
                prev.map((b) => (b.id === id ? { ...b, is_active: true } : b))
            );
            const ok = await runAction(async () => {
                await branchApi.activate(id);
            });
            // Revert on error by refetching
            if (!ok) await fetchBranches();
        },
        [runAction, fetchBranches],
    );

    const deactivateBranch = useCallback(
        async (id: string) => {
            setBranches((prev) =>
                prev.map((b) => (b.id === id ? { ...b, is_active: false } : b))
            );
            const ok = await runAction(async () => {
                await branchApi.deactivate(id);
            });
            if (!ok) await fetchBranches();
        },
        [runAction, fetchBranches],
    );

    return {
        branches,
        setBranches,   // exposed so BranchesTab can merge edits without refetch
        loading,
        error,
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