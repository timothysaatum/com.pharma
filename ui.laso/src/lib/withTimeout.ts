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

export interface TimeoutOptions {
    timeoutMs?: number;
    dataKey?: string;  // unique identifier for this data (e.g., 'inventory:branch123')
}

export interface TimeoutResult<T> {
    data: T;
    isFromCache: boolean;
    cached_at?: string;
    fetched_at?: string;
}

export async function withTimeout<T>(
    serverFn: () => Promise<T>,
    cacheFn: () => Promise<T>,
    options: TimeoutOptions = {}
): Promise<TimeoutResult<T>> {
    const { timeoutMs = 20000, dataKey = "" } = options;

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
