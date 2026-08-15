# Phase 1: Sellable Quantity Published

## Context and Problem Statement
The offline sales manager and UI list previously recalculated sellable quantity from drug_batch records in real-time. This caused issues when batch data had not fully synced to the client but inventory had, leading to incorrect quantity blocking and UI inconsistencies. Furthermore, maintaining duplicated calculation logic on the client violates the goal of centralizing logic.

## Decision
We decided to compute the `sellable_quantity` (as SUM of unexpired, non-zero batch quantities) centrally on the server and project it as a field on `branch_inventory` API responses during synchronization.

1.  **Canonical Implementation**: The server-side logic in `app/services/sync/_sellable_qty.py` dictates the `sellable_quantity`. The API endpoint `BranchInventoryResponse` now includes `sellable_quantity`.
2.  **Client-Side Persistence**: The client local SQLite DB (tauri) was altered to add the `sellable_quantity` column to `branch_inventory`.
3.  **UI Updates**: Functions `getBranchInventory`, `getValuation`, and `getSellableQuantity` in `localRead.ts` have been updated to rely solely on `bi.sellable_quantity` rather than performing complex `COALESCE(SUM(...))` queries on batch records. `getSellableQuantity` preserves a fallback to checking the un-synced cache when `sellable_quantity` is 0 to ensure safety.

## Consequences
- Single source of truth for computed sellable quantity logic.
- Faster, non-blocking queries on the local SQLite DB since batch aggregation is removed.
- Prevents errors caused by partial synchronization states.
