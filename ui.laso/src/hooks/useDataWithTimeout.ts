/**
 * useDataWithTimeout.ts
 * =====================
 * React hook that wraps data fetching with timeout and cache fallback.
 * Makes it easy to integrate timeout fallback into any component.
 *
 * Usage:
 *   const { data, loading, error, isFromCache } = useDataWithTimeout(
 *     () => inventoryApi.getItems(branchId),
 *     () => localRead.getItems(branchId),
 *     { timeoutMs: 10000, dataKey: 'inventory' }
 *   );
 */

import { useEffect, useState } from "react";
import { withTimeout } from "@/lib/withTimeout";

export interface UseDataWithTimeoutOptions {
    timeoutMs?: number;
    dataKey?: string;
}

export interface UseDataWithTimeoutState<T> {
    data: T | null;
    loading: boolean;
    error: Error | null;
    isFromCache: boolean;
    cached_at?: string;
    fetched_at?: string;
    retry: () => void;
}

/**
 * Hooks that fetches data with timeout and cache fallback.
 * @param serverFn - Async function that fetches from server
 * @param cacheFn - Async function that fetches from cache
 * @param options - Configuration (timeoutMs, dataKey)
 * @param deps - Dependencies that trigger refetch
 */
export function useDataWithTimeout<T>(
    serverFn: () => Promise<T>,
    cacheFn: () => Promise<T>,
    options: UseDataWithTimeoutOptions = {},
    deps: React.DependencyList = []
): UseDataWithTimeoutState<T> {
    const [data, setData] = useState<T | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<Error | null>(null);
    const [isFromCache, setIsFromCache] = useState(false);
    const [cached_at, setCached_at] = useState<string | undefined>();
    const [fetched_at, setFetched_at] = useState<string | undefined>();

    const fetchData = async () => {
        setLoading(true);
        setError(null);

        try {
            const result = await withTimeout(
                serverFn,
                cacheFn,
                options
            );

            setData(result.data);
            setIsFromCache(result.isFromCache);
            setCached_at(result.cached_at);
            setFetched_at(result.fetched_at);
        } catch (err) {
            const errorObj = err instanceof Error ? err : new Error(String(err));
            setError(errorObj);
            setData(null);
            setIsFromCache(false);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        let cancelled = false;

        void (async () => {
            await fetchData();
            if (cancelled) {
                setLoading(false);
            }
        })();

        return () => {
            cancelled = true;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, deps);

    return {
        data,
        loading,
        error,
        isFromCache,
        cached_at,
        fetched_at,
        retry: fetchData,
    };
}
