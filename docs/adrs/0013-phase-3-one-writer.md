# 13. Phase 3: One Writer Architecture

Date: 2024-05-18

## Status

Accepted

## Context

The application previously employed a dual-writer architecture for syncing data between the backend and local SQLite database, especially for inventory, drugs, and purchase orders. Data would be written to the local database both via full background syncs (`syncEngine.ts`) and opportunistically from REST API responses (e.g., caching drug search results or purchase orders). This caused significant issues:
1. **Race Conditions**: Parallel updates to the same entities could overwrite each other.
2. **Incomplete Data States**: The opportunistic cache could result in a state where a branch has `branch_inventory` rows but no corresponding `drug_batches` rows, falsely appearing as though all stock had expired. This required workarounds such as the `noBatchData` fallback in `getSellableQuantity`.
3. **Complexity**: Managing schema migrations and writes in multiple places (`localDb.ts`, `localWrite.ts`, `syncEngine.ts`) was hard to reason about.

## Decision

We are moving to a strict "one writer" architecture for `branch_inventory`, `drugs`, and `purchase_orders` in the local cache. 

1. **Delete Dual-Writer Logic**: We removed all opportunistic caching functions that filled SQLite from REST responses:
   - `cacheBranchInventoryRows` in `localDb.ts`
   - `cacheDrugs` in `localDb.ts`
   - `cacheBranchScopedDrugs` in `localDb.ts`
   - `cachePurchaseOrders` in `localWrite.ts`
2. **Simplified Reads**: The `noBatchData` fallback in `localRead.ts` -> `getSellableQuantity` was removed, as partial cache states are no longer possible.
3. **Strict Boundaries**: 
   - `syncEngine.ts` is the single writer for all synchronized server data.
   - `offlineSalesManager.ts` is the single writer for locally-recorded offline sales.
   - All other local database operations for these entities are strictly read-only.

## Consequences

- **Simplified Mental Model**: Developers no longer have to trace multiple write paths to understand how local data is populated.
- **Reliable Data**: The local database state directly reflects either the last successful sync or locally recorded sales, avoiding partial/corrupt states.
- **Removed Workarounds**: Hacks like `noBatchData` are no longer needed to paper over architectural flaws.
- UI components now rely purely on API responses for live data (when online) or the synced SQLite database (when offline), without attempting to cross-pollinate the two.
