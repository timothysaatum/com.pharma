# CRR all-table production dispatch evidence — 2026-07-11

Environment: PostgreSQL 14.23 on ARM64, native cr-sqlite extension, current
`main`. All test records used random IDs/business keys and were deleted in
`finally` cleanup blocks.

## Production merge dispatch

Command:

```bash
PYTHONPATH=. CRSQLITE_EXTENSION_PATH=../ui.laso/src-tauri/crsqlite.so \
  python3.12 tests/e2e_crr_production_dispatch.py
```

Results:

- `branch_inventory`: PASS production `sum_and_merge` (3 + 4 = 7).
- `drug_batches`: PASS production `sum_and_merge` (quantity 7, remaining 5).
- `prescriptions`: PASS production `keep_both_renumber` and idempotent audit.
- `purchase_orders`: PASS production `keep_both_renumber` and idempotent audit.
- `sales`: PASS production `keep_both_renumber` and idempotent audit.

The runner calls `ShadowDB.upsert_merged_row()` directly; it does not duplicate
the merge algorithms in test code.

## Customer production merge

`tests/e2e_crr_customers.py`: PASS against real PostgreSQL:

- three-way phone collision converged to the earliest survivor;
- additive fields folded across all three rows;
- sale and prescription FKs repointed;
- two loser audit/directive records persisted;
- empty phone/email customers remained distinct;
- cleanup completed.

## Real HTTP transport and resilience

The actual FastAPI application was started on port 8011 with real PostgreSQL
and native cr-sqlite, then `/tmp/laso_real_sync_probe.py` exercised the public
CRR endpoints:

- health: 200;
- genuine binary `crsql_changes` push: 200, 11 changes received, one row merged;
- identical retry: 200, zero rows re-applied;
- higher-version negative quantity: rejected without corrupting PostgreSQL;
- authoritative shadow row restored and reconciliation remained safe;
- pull: 12 changes, cursor 3;
- cursor delta pull: zero changes;
- unauthorized branch push: 403;
- isolated rows/session cleaned up.

## Bugs found and corrected

1. Shadow-only `items_json`/`items_count` fields leaked into PostgreSQL inserts
   for sales/purchase orders. Production rows are now filtered to mapped
   PostgreSQL columns.
2. Keep-both business-key discovery used SQLite bracket quoting in PostgreSQL.
   It now uses PostgreSQL-compatible quoted identifiers.
3. Type coercion depended on incidental ORM import order. `ShadowDB` now
   registers mapped models before consulting `Base.metadata`.
