# 0012: Phase 2 Server-Side FEFO Allocation

## Status
Accepted

## Context
In the offline-first sales architecture, the client previously selected the batches to deduct from inventory and sent these decisions in the `sale_created` event payload. This required the server to trust the client's selection, which could lead to incorrect deductions or negative inventory if the client had a stale view of the inventory. In Phase 0, we mitigated cross-tenant deduction issues, but the client still remained authoritative for batch selection.

## Decision
We are changing the sale event payload so it carries drug and quantity (intent), not batch allocations (decision). The server projector runs FEFO (First Expired, First Out) allocation authoritatively during sync.
- The `fefo_allocate` function is used by the server to determine batch allocation based on `authored_at`.
- The client may still include `provisional_batch_allocations` in the payload for receipt printing and local display, but these are ignored by the server for stock deduction.
- The projector supports both payload shapes (v1 with `batch_allocations` and v2 with `provisional_batch_allocations`) to ensure backward compatibility for one release cycle.

## Consequences
- **Positive**: The server is authoritative for batch deduction, preventing negative inventory and incorrect batch allocations from stale clients.
- **Positive**: FEFO logic is centralized on the server.
- **Negative**: The client's local inventory may temporarily differ from the server's authoritative view until the next sync completes.
