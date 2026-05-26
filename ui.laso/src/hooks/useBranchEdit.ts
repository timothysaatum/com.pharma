/**
 * useBranchEdit.ts
 * ─────────────────────────────────────────────────────────────
 * Thin hook wrapping branchApi.update() for the EditBranchPanel.
 * Kept separate from useBranches so the panel owns its own
 * loading/error state without polluting the list state.
 */

import { useState, useCallback } from "react";
import { branchApi } from "@/api/branches";
import { parseApiError, isOfflineError, isBackendReachable } from "@/api/client";
import type { BranchUpdate, Branch } from "@/types";

export function useBranchEdit() {
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);

    const updateBranch = useCallback(
        async (id: string, data: BranchUpdate): Promise<Branch | null> => {
            if (!isBackendReachable()) {
                setSaveError("Cannot save branch changes while offline. Reconnect and try again.");
                return null;
            }
            setSaving(true);
            setSaveError(null);
            try {
                const updated = await branchApi.update(id, data);
                return updated;
            } catch (err) {
                const message = isOfflineError(err)
                    ? "Cannot save branch changes while offline. Reconnect and try again."
                    : parseApiError(err);
                setSaveError(message);
                return null;
            } finally {
                setSaving(false);
            }
        },
        [],
    );

    return {
        saving,
        saveError,
        clearSaveError: () => setSaveError(null),
        updateBranch,
    };
}