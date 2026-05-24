/**
 * usePurchaseOrderDetail.ts
 * ─────────────────────────────────────────────────────────────
 * Manages state for the PO detail / receive-goods page.
 *
 *  - Loads a single PurchaseOrderWithDetails
 *  - Exposes receiveGoods(), addItems(), updateItem()
 *  - All mutations refresh the PO from the server on success
 */

import { useState, useEffect, useCallback } from "react";
import { purchaseOrdersApi } from "@/api/purchases";
import { parseApiError } from "@/api/client";
import type {
    PurchaseOrderWithDetails,
    PurchaseOrderItemCreate,
    ReceivePurchaseOrder,
    ReceivePurchaseOrderResponse,
} from "@/types";

export function usePurchaseOrderDetail(poId: string | null) {
    const [po, setPo] = useState<PurchaseOrderWithDetails | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [mutating, setMutating] = useState(false);
    const [mutateError, setMutateError] = useState<string | null>(null);

    // ── Load ──────────────────────────────────────────────────
    const load = useCallback(async () => {
        if (!poId) return;
        setLoading(true);
        setError(null);
        try {
            const data = await purchaseOrdersApi.get(poId);
            setPo(data);
        } catch (err) {
            setError(parseApiError(err));
        } finally {
            setLoading(false);
        }
    }, [poId]);

    useEffect(() => {
        load();
    }, [load]);

    // ── Generic mutation wrapper ──────────────────────────────
    const mutate = useCallback(
        async <T>(fn: () => Promise<T>): Promise<T | null> => {
            setMutating(true);
            setMutateError(null);
            try {
                const result = await fn();
                await load(); // always re-fetch for fresh server state
                return result;
            } catch (err) {
                setMutateError(parseApiError(err));
                return null;
            } finally {
                setMutating(false);
            }
        },
        [load],
    );

    // ── Receive goods ─────────────────────────────────────────
    const receiveGoods = useCallback(
        (data: ReceivePurchaseOrder): Promise<ReceivePurchaseOrderResponse | null> =>
            mutate(() => purchaseOrdersApi.receiveGoods(poId!, data)),
        [mutate, poId],
    );

    // ── Add items to draft PO ─────────────────────────────────
    const addItems = useCallback(
        (items: PurchaseOrderItemCreate[]) =>
            mutate(() => purchaseOrdersApi.addItems(poId!, items)),
        [mutate, poId],
    );

    // ── Update a single item ──────────────────────────────────
    const updateItem = useCallback(
        (itemId: string, quantity_ordered: number, unit_cost: number) =>
            mutate(() =>
                purchaseOrdersApi.updateItem(poId!, itemId, quantity_ordered, unit_cost),
            ),
        [mutate, poId],
    );

    return {
        po,
        loading,
        error,
        mutating,
        mutateError,
        clearMutateError: () => setMutateError(null),
        receiveGoods,
        addItems,
        updateItem,
        refresh: load,
    };
}