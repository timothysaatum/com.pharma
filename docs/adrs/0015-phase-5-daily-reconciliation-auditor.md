# 0015. Phase 5 - Daily Reconciliation Auditor

## Status
Accepted

## Context
As part of the Conserved-Quantities Architecture plan (Phase 5), we need an automated inventory reconciliation auditor service. This service compares various stock indicators to detect unexplained inventory drift or dead-letter sync failures. This ensures that the invariant rules around quantities are constantly audited and that off-by-one errors or sync collisions are caught early.

## Decision
We implemented a backend reconciliation service (`generate_reconciliation_report`) that:
1. Compares `branch_inventory.quantity` against the sum of `DrugBatch.remaining_quantity` per drug.
2. Compares `branch_inventory.sellable_quantity` (quantity - reserved) against unleased sellable quantities (sellable - (active leases)).
3. Checks the raw `event_dead_letter` SQL table for any dead-letter queue (poison pill) messages related to the organization.
4. Reports drift metrics and categorizes mismatches (`balanced`, `batch_mismatch`, `sellable_mismatch`).

A daily CLI script `scripts/reconcile_branch.py` was created to run these checks natively via CRON and output JSON reports to `/tmp/reconciliation/`.
We also implemented an API route (`GET /api/v1/inventory/branch/{branch_id}/reconciliation/report`) and a corresponding frontend UI panel.

## Consequences
- Better visibility into data anomalies (if any).
- Prevents silent drift of quantities between batches, branches, and offline nodes.
- Exposes dead letter events directly in the admin dashboard.
