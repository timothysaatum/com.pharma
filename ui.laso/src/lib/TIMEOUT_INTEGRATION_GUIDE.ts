/**
 * TIMEOUT FALLBACK INTEGRATION GUIDE
 * ==================================
 *
 * This guide shows how to integrate timeout-based cache fallback into your pages.
 * When the server takes too long to respond (default 20s), the app automatically
 * shows cached data instead of an empty loading state.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * BASIC INTEGRATION (useDataWithTimeout hook)
 * ──────────────────────────────────────────────────────────────────────────
 *
 * Example: InventoryPage.tsx
 *
 * import { useDataWithTimeout } from "@/hooks/useDataWithTimeout";
 * import { DataFreshnessIndicator } from "@/components/DataFreshnessIndicator";
 * import { inventoryApi } from "@/api/inventory";
 * import { localRead } from "@/lib/localRead";
 *
 * function InventoryPage() {
 *   const branchId = useAuthStore((s) => s.user?.branch_id);
 *
 *   const { data, loading, error, isFromCache, retry } = useDataWithTimeout(
 *     // Server fetch function
 *     () => inventoryApi.getItems(branchId),
 *     // Cache fallback function
 *     () => localRead.getInventoryItems(branchId),
 *     // Options
 *     {
 *       timeoutMs: 15000,  // 15 seconds (default 20s)
 *       dataKey: `inventory:${branchId}`,
 *     },
 *     // Re-fetch when branchId changes
 *     [branchId]
 *   );
 *
 *   return (
 *     <>
 *       {isFromCache && (
 *         <DataFreshnessIndicator isFromCache compact />
 *       )}
 *       {loading && !data && <LoadingSpinner />}
 *       {data && <InventoryTable items={data} />}
 *       {error && !data && (
 *         <ErrorAlert
 *           message="Failed to load inventory"
 *           onRetry={retry}
 *         />
 *       )}
 *     </>
 *   );
 * }
 *
 * ──────────────────────────────────────────────────────────────────────────
 * ADVANCED: withTimeout utility (manual control)
 * ──────────────────────────────────────────────────────────────────────────
 *
 * For more control over timeout behavior, use withTimeout directly:
 *
 * import { withTimeout } from "@/lib/withTimeout";
 *
 * async function loadData() {
 *   try {
 *     const result = await withTimeout(
 *       // Server function
 *       () => inventoryApi.getItems(branchId),
 *       // Cache function
 *       () => localRead.getInventoryItems(branchId),
 *       // Options
 *       {
 *         timeoutMs: 10000,
 *         dataKey: `inventory:${branchId}`,
 *       }
 *     );
 *
 *     console.log(`Data from cache: ${result.isFromCache}`);
 *     console.log(`Cached at: ${result.cached_at}`);
 *     // Use result.data
 *   } catch (err) {
 *     console.error("Both server and cache failed:", err);
 *   }
 * }
 *
 * ──────────────────────────────────────────────────────────────────────────
 * TIMEOUT DURATIONS
 * ──────────────────────────────────────────────────────────────────────────
 *
 * Recommended timeouts by operation:
 *
 *   Page load (inventory, sales, etc.)   → 10-15 seconds
 *   API searches/filters                 → 8-10 seconds
 *   Background sync operations           → 25-30 seconds
 *   Small data fetches (drugs)           → 5-8 seconds
 *   Large report generation              → 30+ seconds
 *
 * Default: 20 seconds (good for most cases)
 *
 * ──────────────────────────────────────────────────────────────────────────
 * DATA FRESHNESS TRACKING
 * ──────────────────────────────────────────────────────────────────────────
 *
 * The system tracks which data is fresh vs cached via the dataFreshnessStore:
 *
 * import { dataFreshnessStore } from "@/stores/dataFreshnessStore";
 *
 * // Get freshness info for a data key
 * const freshness = dataFreshnessStore((s) => s.getFreshness('inventory:branch123'));
 * if (freshness?.isFromCache) {
 *   console.log('Data is from cache, last synced:', freshness.cached_at);
 * }
 *
 * ──────────────────────────────────────────────────────────────────────────
 * VISUAL INDICATORS
 * ──────────────────────────────────────────────────────────────────────────
 *
 * Use DataFreshnessIndicator to show users when data is from cache:
 *
 * // Compact version (small badge inline)
 * <DataFreshnessIndicator
 *   isFromCache={isFromCache}
 *   cached_at={cached_at}
 *   compact
 * />
 *
 * // Full version (larger info box)
 * <DataFreshnessIndicator
 *   isFromCache={isFromCache}
 *   cached_at={cached_at}
 *   error={error?.message}
 * />
 *
 * ──────────────────────────────────────────────────────────────────────────
 * COMMON PATTERNS
 * ──────────────────────────────────────────────────────────────────────────
 *
 * Pattern 1: Page load with cache fallback
 * -----------------------------------------
 * function MyPage() {
 *   const { data, loading, isFromCache } = useDataWithTimeout(
 *     () => api.getData(),
 *     () => localRead.getData(),
 *     { dataKey: 'mypage:data', timeoutMs: 15000 }
 *   );
 *
 *   if (loading && !data) return <Skeleton />;
 *   return (
 *     <>
 *       {isFromCache && <CacheBadge />}
 *       <DataDisplay data={data} />
 *     </>
 *   );
 * }
 *
 * Pattern 2: Search with timeout
 * ------
 * function SearchBox() {
 *   const [query, setQuery] = useState("");
 *   const { data: results } = useDataWithTimeout(
 *     () => api.search(query),
 *     () => localRead.searchCache(query),
 *     { dataKey: `search:${query}`, timeoutMs: 5000 },
 *     [query]  // Re-fetch when query changes
 *   );
 *
 *   return (
 *     <>
 *       <input onChange={(e) => setQuery(e.target.value)} />
 *       {results && <ResultsList items={results} />}
 *     </>
 *   );
 * }
 *
 * Pattern 3: Conditional timeout (by connection quality)
 * -------------------------------------------------------
 * function SmartFetch() {
 *   const isSlowConnection = useIsSlowConnection();  // Your hook
 *   const timeout = isSlowConnection ? 5000 : 15000;
 *
 *   const { data } = useDataWithTimeout(
 *     () => api.getData(),
 *     () => localRead.getData(),
 *     { timeoutMs: timeout }
 *   );
 * }
 *
 * ──────────────────────────────────────────────────────────────────────────
 * TESTING TIMEOUT BEHAVIOR
 * ──────────────────────────────────────────────────────────────────────────
 *
 * To test timeout fallback locally:
 *
 * 1. Add artificial delay in your API client:
 *    apiClient.interceptors.response.use(
 *      (response) => {
 *        // Simulate slow network
 *        return new Promise(resolve => setTimeout(() => resolve(response), 25000));
 *      }
 *    );
 *
 * 2. Or use browser DevTools: Network tab → Throttling → Slow 3G
 *
 * 3. Set a very short timeout for testing:
 *    useDataWithTimeout(serverFn, cacheFn, { timeoutMs: 2000 })
 *
 * 4. Verify DataFreshnessIndicator appears when cache is used
 *
 * ──────────────────────────────────────────────────────────────────────────
 * EDGE CASES
 * ──────────────────────────────────────────────────────────────────────────
 *
 * ✓ Both server and cache fail?
 *   → Error is thrown, user sees error message + retry button
 *
 * ✓ Cache has old data but server succeeds?
 *   → Server data wins, isFromCache = false, badge doesn't show
 *
 * ✓ Server succeeds quickly, then times out on next call?
 *   → Next call uses fresh server data, no cache badge
 *
 * ✓ Offline on first load?
 *   → Timeout triggers immediately, cache is shown
 *
 */

// This file is documentation only. No code to run.
