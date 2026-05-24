/**
 * useBranchEdit.ts
 * ─────────────────────────────────────────────────────────────
 * Thin hook wrapping branchApi.update() for the EditBranchPanel.
 * Kept separate from useBranches so the panel owns its own
 * loading/error state without polluting the list state.
 */

import { useState, useCallback } from "react";
import { branchApi } from "@/api/branches";
import { parseApiError } from "@/api/client";
import type { BranchUpdate, Branch } from "@/types";

export function useBranchEdit() {
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);

    const updateBranch = useCallback(
        async (id: string, data: BranchUpdate): Promise<Branch | null> => {
            setSaving(true);
            setSaveError(null);
            try {
                const updated = await branchApi.update(id, data);
                return updated;
            } catch (err) {
                setSaveError(parseApiError(err));
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