# ADR 0014: Phase 4 - Stock Leases for Offline Gating

## Status
Accepted

## Context
When an offline-first point-of-sale terminal sells stock, it subtracts quantities from its local replica of the `branch_inventory` table. Since multiple terminals can operate offline concurrently, they may collectively sell more stock than the branch actually owns if they all sell against the same globally available inventory count. The server's sync tracking architecture handles CRR row merging natively, but it cannot prevent offline overselling when conflicts are merged asynchronously.

## Decision
We introduce a **Stock Lease** as a first-class aggregate that partitions the branch's central pool of sellable inventory among its connected terminals. 

1. **Backend Partitioning**: The `compute_sellable_quantities` engine has been updated to subtract active, unexpired leases assigned to *other* terminals from the total sellable quantity. This ensures that the `sellable_quantity` synchronized to the CRR table represents the unleased pool.
2. **Lease Lifecycle**: A new `StockLease` model and `LeaseService` manage leasing operations using PostgreSQL `SELECT ... FOR UPDATE` row locks to prevent race conditions during concurrent lease acquisition.
3. **Frontend Gating**: The frontend implements a background `leaseEngine` to periodically acquire leases when online. 
4. **Offline Selling Rules**: When computing available local stock in `localRead.getSellableQuantity()`, the terminal evaluates its own active leases (`leaseRemaining`). If online, it may optimistically display `leaseRemaining + unleasedPool`. If offline, it *only* displays and permits sales against `leaseRemaining`, fundamentally making offline multi-terminal overselling impossible.

## Consequences
* **Positive**: Absolute protection against multi-terminal offline overselling. Each terminal holds a cryptographic promise of available stock.
* **Negative**: In environments with highly volatile connectivity, terminals might hoard stock in leases, preventing other terminals from accessing it until the lease expires. A TTL logic and `expire_stale_leases` CRON job manages this risk.
* **Positive**: Maintains architectural parity and does not break the existing CRR sync behavior; leases are independent, non-CRR aggregates fetched asynchronously.
