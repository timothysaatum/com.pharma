# ADR 0010: Cross-Tenant Batch Deduction Fix (Phase 0)

## Context
During the audit of our synchronization projectors, we discovered a vulnerability in the offline sale sync path. The `_apply_item` and `_apply_voided` logic in `SaleProjector` updated `drug_batches` based solely on the provided `batch_id`. Since UUIDs were assumed to be universally unique, the queries did not scope the `UPDATE` statements by `branch_id` or `organization_id` (or even `drug_id` correctly in all cases).

A malicious or misconfigured terminal could emit a `sale_created` event containing another tenant's `batch_id`, thereby wrongfully deducting inventory from a batch belonging to a completely different organization. This violated our strict multi-tenant isolation guarantees.

## Decision
We updated the `UPDATE drug_batches` and related `SELECT` queries in `app/services/sync/eventlog/projectors/sale.py` to enforce tenancy boundaries directly at the database level. Specifically, we added:
- `AND branch_id = CAST(:branch_id AS UUID)`
- `AND drug_id = CAST(:drug_id AS UUID)`

to the `WHERE` clauses of the batch deduction logic.

Additionally, to ensure schema parity going forward and catch any undocumented schema changes that might bypass our autogeneration tests, we introduced a SQLite-backed automated test suite (`tests/unit/test_schema_parity.py`). Finally, we wired up PostgreSQL as a dedicated service in our GitHub Actions (`ci.yml`) to guarantee that all integration tests reflect production constraints.

## Status
Accepted

## Consequences
- **Security**: Cross-tenant inventory manipulation via event logs is structurally blocked.
- **Resilience**: Any attempt to modify a batch not owned by the branch will result in a `ValueError` with no rows updated.
- **Observability**: A dedicated `test_cross_tenant_batch_guard.py` automated test enforces this rule. Schema parity errors are now loud and visible in CI via `test_schema_parity.py`.
