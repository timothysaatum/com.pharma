/**
 * withTimeout.ts
 * ==============
 * Wraps API calls with configurable timeout and automatic cache fallback.
 * If server is slow, falls back to local cache after timeout expires.
 *
 * Usage:
 *   const data = await withTimeout(
 *     () => inventoryApi.getItems(branchId),
 *     () => localRead.getItems(branchId),
 *     { timeoutMs: 8000, dataKey: 'branch_inventory' }
 *   );
 *   // Returns: { data, isFromCache: boolean, cached_at?: string }
 */

import { dataFreshnessStore } from "@/stores/dataFreshnessStore";
import { isBackendKnownUnreachable } from "@/api/client";

export interface TimeoutOptions {
    timeoutMs?: number;
    dataKey?: string;  // unique identifier for this data (e.g., 'inventory:branch123')
}

export interface TimeoutResult<T> {
    data: T;
    isFromCache: boolean;
    cached_at?: string;
    fetched_at?: string;
    /**
     * The error that caused the fallback, when one did.
     *
     * `withTimeout` catches EVERY error from the server call — not just
     * timeouts and connection failures — and returns the local cache instead.
     * That is the right default for a slow backend, but it made a 422 from a
     * server-side data fault indistinguishable from an empty result: the page
     * rendered "No drugs found" with no signal that the request had failed.
     *
     * Callers that need to tell the user the request failed read this and
     * surface it (see DrugListPage's error state + Retry). Additive only: a
     * caller that ignores it behaves exactly as before.
     */
    fallbackError?: Error;
}

export async function withTimeout<T>(
    serverFn: () => Promise<T>,
    cacheFn: () => Promise<T>,
    options: TimeoutOptions = {}
): Promise<TimeoutResult<T>> {
    const { timeoutMs = 20000, dataKey = "" } = options;

    // Check both navigator.onLine and backendReachable for robust offline detection.
    // The main.tsx probe ensures backendReachable is accurate before render,
    // but navigator.onLine is an instant, synchronous check that works even
    // if the probe hasn't completed or state is stale.
    if (isBackendKnownUnreachable() || (typeof navigator !== "undefined" && !navigator.onLine)) {
        const cachedData = await cacheFn();
        if (dataKey) {
            dataFreshnessStore.setState((state) => ({
                freshData: {
                    ...state.freshData,
                    [dataKey]: {
                        isFromCache: true,
                        cached_at: new Date().toISOString(),
                    },
                },
            }));
        }
        return {
            data: cachedData,
            isFromCache: true,
            cached_at: new Date().toISOString(),
        };
    }

    let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

    try {
        const result = await Promise.race([
            serverFn(),
            new Promise<T>((_, reject) => {
                timeoutHandle = setTimeout(() => {
                    reject(new Error(`Timeout after ${timeoutMs}ms`));
                }, timeoutMs);
            }),
        ]);

        if (timeoutHandle) {
            clearTimeout(timeoutHandle);
        }

        if (dataKey) {
            dataFreshnessStore.setState((state) => ({
                freshData: {
                    ...state.freshData,
                    [dataKey]: {
                        isFromCache: false,
                        fetched_at: new Date().toISOString(),
                    },
                },
            }));
        }

        return {
            data: result,
            isFromCache: false,
            fetched_at: new Date().toISOString(),
        };
    } catch (err) {
        if (timeoutHandle) {
            clearTimeout(timeoutHandle);
        }

        try {
            const cachedData = await cacheFn();

            if (dataKey) {
                dataFreshnessStore.setState((state) => ({
                    freshData: {
                        ...state.freshData,
                        [dataKey]: {
                            isFromCache: true,
                            cached_at: new Date().toISOString(),
                            error: err instanceof Error ? err.message : "Unknown error",
                        },
                    },
                }));
            }

            return {
                data: cachedData,
                isFromCache: true,
                cached_at: new Date().toISOString(),
                fallbackError: err instanceof Error ? err : new Error(String(err)),
            };
        } catch (cacheErr) {
            if (dataKey) {
                dataFreshnessStore.setState((state) => ({
                    freshData: {
                        ...state.freshData,
                        [dataKey]: {
                            isFromCache: false,
                            error: `Server timeout + cache read failed: ${
                                cacheErr instanceof Error ? cacheErr.message : "Unknown"
                            }`,
                        },
                    },
                }));
            }

            throw cacheErr;
        }
    }
}

export async function withSyncTimeout<T>(
    serverFn: () => Promise<T>,
    cacheFn: () => Promise<T>,
    label: string = "sync"
): Promise<TimeoutResult<T>> {
    return withTimeout(serverFn, cacheFn, {
        timeoutMs: 30000,
        dataKey: `sync:${label}:${new Date().toISOString()}`,
    });
}
