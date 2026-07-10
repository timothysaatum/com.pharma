# 0005: Purchase-Order Branch Authorization Gap

**Status:** Accepted and implemented

**Date:** 2026-07-10

**Context:** Discovered incidentally during CRR audit-search work

## Finding

`GET /purchase-orders/` does not validate that a supplied `branch_id` belongs to
the authenticated user's `assigned_branches`. Any `branch_id` within the user's
organization is accepted and queried as-is.

This is inconsistent with the sales and prescriptions endpoints, which enforce
assigned-branch scope.

When `branch_id` is omitted, the endpoint returns purchase orders from all
branches in the authenticated user's organization without assigned-branch
filtering.

## Decision

Purchase-order listing remains organization-scoped and branch access is
determined as follows:

- A supplied `branch_id` is allowed when it is assigned to the user.
- A supplied unassigned `branch_id` returns HTTP 403 for a non-elevated user.
- When `branch_id` is omitted, a non-elevated user sees the aggregate across
  assigned branches only.
- A non-elevated user with no assignments receives an empty HTTP 200 result.
- Elevated users retain organization-wide access when the branch is omitted and
  may select any branch in their organization.

The established dynamic permission model is used for elevated access. A user is
elevated when `is_super_admin` is true or effective inherited permissions include
`approve_purchase_orders` (procurement), `view_reports` (finance/reporting), or
the wildcard permission. No new permission was introduced.

## Implementation

- `GET /purchase-orders/` enforces the decision before pagination.
- The existing organization predicate remains mandatory in every case.
- `PurchasesPage.tsx` distinguishes a genuinely unassigned non-elevated user
  from an empty purchase-order list and directs them to contact an administrator.
- A multi-branch user with no active branch selection continues to omit
  `branch_id`, which now returns the assigned-branch aggregate.

## Test Coverage

Backend tests cover valid, invalid, elevated, assigned aggregate, empty assigned,
and organization-wide cases. Frontend tests cover the unassigned message, the
multi-branch/no-active-selection aggregate request, and the elevated empty-list
state. This remains a standalone authorization fix, separate from CRR migration.
