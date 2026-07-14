# CRR Real-Postgres E2E Evidence: branch_inventory and drug_batches

**Run date:** 2026-07-10 14:27 UTC

**Branch:** `spike/rusqlite-swap`

**Base commit:** `d462ef97c28ae3f46ca6a43523b51c37120a5770`

**Database:** PostgreSQL 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1), real local PostgreSQL via `asyncpg`

**cr-sqlite extension:** `crsqlite/linux-aarch64/crsqlite.so`

The working tree contained the in-progress CRR migration changes when these tests
were run. This report intentionally records the base commit as well as that fact,
because the migration work had not yet been committed as a single revision.

## branch_inventory

Command, run from `backend.laso/`:

```sh
set -a
source .env
export CRSQLITE_EXTENSION_PATH=/home/ubuntu/projects/com.pharma/crsqlite/linux-aarch64/crsqlite.so
python3.12 tests/e2e_crr_sync_pg.py
```

Result: **PASS** (exit code 0)

```text
2026-07-10 14:27:02,508 [INFO] e2e_crr_sync_pg: Postgres OK (PostgreSQL 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1) on aarch64-unknown-linux-gnu)
2026-07-10 14:27:02,515 [INFO] e2e_crr_sync_pg: SCENARIO 4: BLOB serialisation roundtrip
2026-07-10 14:27:02,515 [INFO] e2e_crr_sync_pg:   ✅ Scenario 4 PASSED
2026-07-10 14:27:02,515 [INFO] e2e_crr_sync_pg: SCENARIO 1: Postgres upsert of merged row
2026-07-10 14:27:02,520 [INFO] e2e_crr_sync_pg:   ✅ Postgres ON CONFLICT: qty=150 location=Bin-3 (type casting OK)
2026-07-10 14:27:02,520 [INFO] e2e_crr_sync_pg:   ✅ Scenario 1 PASSED
2026-07-10 14:27:02,522 [INFO] e2e_crr_sync_pg: SCENARIO 2: Duplicate business-key — Postgres
2026-07-10 14:27:02,530 [INFO] e2e_crr_sync_pg:   ✅ Merge: qty=50+30=80, location=Bin-2 (newer), id=first-arrived
2026-07-10 14:27:02,530 [INFO] e2e_crr_sync_pg:   ✅ Scenario 2 PASSED
2026-07-10 14:27:02,531 [INFO] e2e_crr_sync_pg: SCENARIO 3: Crash recovery (reconcile_table) — Postgres
2026-07-10 14:27:02,534 [INFO] e2e_crr_sync_pg:   ✅ Recovery: qty=200 location=Crash-Shelf
2026-07-10 14:27:02,534 [INFO] e2e_crr_sync_pg:   ✅ Scenario 3 PASSED
2026-07-10 14:27:02,536 [INFO] e2e_crr_sync_pg: SCENARIO 5: Postgres type coercion
2026-07-10 14:27:02,539 [INFO] e2e_crr_sync_pg:   ✅ selling_price=19.99 → NUMERIC(10,2)
2026-07-10 14:27:02,539 [INFO] e2e_crr_sync_pg:   ✅ updated_at/created_at TEXT → timestamptz
2026-07-10 14:27:02,539 [INFO] e2e_crr_sync_pg:   ✅ Scenario 5 PASSED
2026-07-10 14:27:02,540 [INFO] e2e_crr_sync_pg: SCENARIO 6: FK constraint enforcement
2026-07-10 14:27:02,542 [INFO] e2e_crr_sync_pg:   ✅ FK violation correctly rejected
2026-07-10 14:27:02,542 [INFO] e2e_crr_sync_pg:   ✅ Scenario 6 PASSED
2026-07-10 14:27:02,543 [INFO] e2e_crr_sync_pg:   ALL SCENARIOS PASSED ✅ (real Postgres)
```

## drug_batches

Command, run from `backend.laso/`:

```sh
set -a
source .env
export CRSQLITE_EXTENSION_PATH=/home/ubuntu/projects/com.pharma/crsqlite/linux-aarch64/crsqlite.so
python3.12 tests/e2e_crr_drug_batches.py
```

Result: **PASS** (exit code 0)

```text
2026-07-10 14:27:13,909 [INFO] e2e_crr_drug_batches: Postgres OK (PostgreSQL 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1) on aarch64-unknown-linux-gnu)
2026-07-10 14:27:13,915 [INFO] e2e_crr_drug_batches: SCENARIO 1: Postgres upsert of merged drug_batches row
2026-07-10 14:27:13,921 [INFO] e2e_crr_drug_batches:   ✅ ON CONFLICT upsert: qty=100, remaining=100, price=12.50
2026-07-10 14:27:13,921 [INFO] e2e_crr_drug_batches:   ✅ Scenario 1 PASSED
2026-07-10 14:27:13,925 [INFO] e2e_crr_drug_batches: SCENARIO 2: Duplicate business-key (sum_and_merge)
2026-07-10 14:27:13,933 [INFO] e2e_crr_drug_batches:   ✅ Sum: qty=80, remaining=78
2026-07-10 14:27:13,933 [INFO] e2e_crr_drug_batches:   ✅ Newest-wins: supplier=Supplier B, cost_price=4.50
2026-07-10 14:27:13,933 [INFO] e2e_crr_drug_batches:   ✅ Winner id=first-arrived
2026-07-10 14:27:13,933 [INFO] e2e_crr_drug_batches:   ✅ Scenario 2 PASSED
2026-07-10 14:27:13,935 [INFO] e2e_crr_drug_batches: SCENARIO 3: Crash recovery
2026-07-10 14:27:13,937 [INFO] e2e_crr_drug_batches:   ✅ Recovery: qty=200, supplier=Crash Supplier
2026-07-10 14:27:13,937 [INFO] e2e_crr_drug_batches:   ✅ Scenario 3 PASSED
2026-07-10 14:27:13,939 [INFO] e2e_crr_drug_batches: SCENARIO 4: Type coercion
2026-07-10 14:27:13,942 [INFO] e2e_crr_drug_batches:   ✅ cost_price=7.50, selling_price=15.99
2026-07-10 14:27:13,942 [INFO] e2e_crr_drug_batches:   ✅ timestamps → timestamptz
2026-07-10 14:27:13,942 [INFO] e2e_crr_drug_batches:   ✅ Scenario 4 PASSED
2026-07-10 14:27:13,944 [INFO] e2e_crr_drug_batches:   ALL drug_batches SCENARIOS PASSED ✅
```

## Scope note

These are the repository's existing real-Postgres suites for the two tables.
They exercise PostgreSQL through `asyncpg` and cr-sqlite where applicable. Their
Postgres upsert helpers mirror the production merge behavior; unlike the customer
suite, they do not invoke `ShadowDB.upsert_merged_row()` directly. This limitation
is recorded here so the evidence is not mistaken for broader production-dispatch
coverage than the suites actually provide.
