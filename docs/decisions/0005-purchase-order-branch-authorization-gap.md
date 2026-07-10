# 0005: Purchase-Order Branch Authorization Gap

**Status:** Finding recorded — decision pending; not fixed

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

## Current Status

This authorization behavior is **not fixed**.

A candidate change was written during the CRR audit-discoverability work. It
would have:

- validated an explicitly supplied `branch_id` against `assigned_branches`;
- returned the user's assigned branches when `branch_id` was omitted;
- returned no rows for a non-super-admin with no assigned branches; and
- retained organization-wide results for a super-admin.

That candidate change was reverted and is not part of the CRR migration.

## Why the Candidate Fix Was Reverted

- No test coverage currently defines the intended no-branch or explicit-branch
  behavior.
- The intended product behavior is undocumented. Plausible policies include:
  organization-wide access, assigned-branch-only access, permission-gated
  cross-branch access, or requiring an explicit active branch.
- The frontend currently relies on omitted-branch behavior when no active branch
  is selected. `PurchasesPage.tsx` calls `usePurchaseOrders()` with
  `activeBranchId ?? undefined`, and `usePurchaseOrders.ts` omits `branch_id`
  when that value is absent. Changing the endpoint default without accounting
  for this flow could regress the purchase-order page for multi-branch or
  not-yet-selected users.

## Next Step

Obtain an explicit product and security decision covering both cases:

1. a request with an explicit `branch_id`; and
2. a request with no `branch_id`.

Then add endpoint and frontend tests that pin down the approved behavior before
implementing it. This work must be handled separately from the CRR migration.
