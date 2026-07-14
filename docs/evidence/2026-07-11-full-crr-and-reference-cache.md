# Full CRR Routing and Reference Cache Evidence

Date: 2026-07-11

## Summary

The desktop sync loop no longer routes application data through legacy
`/sync/pull` when no legacy tables remain. Remaining catalog/reference/audit
tables are CRR-registered and server-authored Postgres rows are published into
the server shadow CRR peer with row-hash idempotency.

Stable branch/organization/user cache entries now carry TTL metadata while
remaining backward compatible with older raw cached values. Offline fallback can
use expired cache entries; online flows still refresh from the backend.

## Real PostgreSQL Verification

Command:

```bash
set -a
. .env
set +a
PYTHONPATH=. CRSQLITE_EXTENSION_PATH=../crsqlite/linux-aarch64/crsqlite.so \
  python3.12 tests/e2e_crr_production_dispatch.py
```

Result: PASS against real PostgreSQL.

Observed pass lines:

```text
PASS branch_inventory: production sum_and_merge
PASS drug_batches: production sum_and_merge
PASS prescriptions: production keep_both_renumber + audit
PASS purchase_orders: production keep_both_renumber + audit
PASS sales: production keep_both_renumber + audit
```

## Server-Authored CRR Publishing

Command:

```bash
set -a
. .env
set +a
PYTHONPATH=. CRSQLITE_EXTENSION_PATH=../crsqlite/linux-aarch64/crsqlite.so \
  python3.12 /tmp/crr_server_authoritative_scale_check.py
```

Result: PASS against real PostgreSQL.

Observed output:

```text
postgres_counts {'drugs': 0, 'drug_categories': 0, 'price_contracts': 0, 'audit_logs': 39}
first_publish_changed_rows 39
second_publish_changed_rows 0
crr_max_db_version 39
crr_changes_returned 624
```

## Thousand-Record Sync Check

Command created 1,200 temporary `audit_logs` rows in real PostgreSQL, published
them into the shadow CRR peer, verified idempotency, and deleted the temporary
rows. This scenario was rerun on 2026-07-12 after adding split CRR push/pull
cursors and paginated CRR pull draining.

```bash
set -a
. .env
set +a
PYTHONPATH=. CRSQLITE_EXTENSION_PATH=../crsqlite/linux-aarch64/crsqlite.so \
  python3.12 /tmp/crr_thousand_record_check.py
```

Result: PASS against real PostgreSQL.

Observed output:

```text
inserted_temp_rows 1200
first_publish_changed_rows 1239
second_publish_changed_rows 0
shadow_temp_audit_rows 1200
crr_changes_returned 19824
```

The 1,200 temporary rows were cleaned up by the script.

## Edge Cases Found and Fixed on 2026-07-12

1. CRR cursor coupling

   The client previously used one `crr_db_version` key for both local push
   progress and remote pull progress. A large server-authored pull could advance
   that shared cursor past later local edits, causing local CRR changes to be
   skipped on push. The client now uses `crr_push_db_version` and
   `crr_pull_db_version`, with read-only fallback to the old key for existing
   installs until the new keys are written.

2. Paginated CRR pulls

   The client previously performed a single `/sync/crr-pull` request per sync
   cycle. Large initial syncs could require several cycles to drain. The client
   now loops while `has_more` is true and cursor progress is made, with a guard
   against tight loops if the server reports `has_more` without advancing the
   cursor.

3. CRR pull branch authorization

   `/sync/crr-pull` now validates `request.branch_id` against the authenticated
   user's assigned branches, matching `/sync/crr-push` and legacy `/sync/pull`.

Additional focused tests:

```text
tests/unit/test_sync_service.py: 11 passed
src/lib/__tests__/crrAuditSync.test.ts + syncTableRouting + migrationV16: 8 passed
```

## Frontend Verification

Focused routing tests:

```bash
pnpm --dir ui.laso exec vitest run \
  src/lib/__tests__/syncTableRouting.test.ts \
  src/lib/__tests__/localDbMigrationV16.test.ts
```

Result:

```text
Test Files  2 passed (2)
Tests       4 passed (4)
```

Production build:

```bash
pnpm --dir ui.laso build
```

Result:

```text
tsc && vite build
✓ built in 14.80s
```

## Notes

An attempted broader backend unit test command printed progress dots but did
not complete in a reasonable time in this session, so it was interrupted. The
modified backend modules were successfully imported with the backend `.env`
loaded:

```text
imports ok
```
