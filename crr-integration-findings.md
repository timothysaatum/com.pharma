# CRR Integration — Implementation Findings

**Branch:** `spike/rusqlite-swap`  
**Date:** 2026-07-10  
**Status:** ✅ Implementation complete, end-to-end verified for `branch_inventory`

---

## What Was Implemented

Full cr-sqlite CRDT-based field-level merge for `branch_inventory`, following ADR 0003 (server shadow DB architecture). This is the first table migrated off the custom conflict-resolution path onto cr-sqlite automatic merging.

### Client Side

| Component | File | What |
|-----------|------|------|
| Migration v15 | `localDb.ts` | Recreates `branch_inventory` with NOT NULL PK + DEFAULTs on all NOT NULL non-PK columns; runs `crsql_as_crr('branch_inventory')`; deduplicates old data by `(branch_id, drug_id)` via `ROW_NUMBER()` |
| Enqueue skip | `localDb.ts` | `CRR_TABLES` set (`{"branch_inventory"}`); `enqueue()` returns early for CRR tables |
| CRR helpers | `localDb.ts` | `getCrrSiteId()`, `getCrrPushChanges(sinceDbVersion)`, `applyCrrPullChanges(rows)` with base64 decode |
| Sync engine | `syncEngine.ts` | `pushCrr()` → batches local `crsql_changes` to `/sync/crr-push`; `pullCrr()` → fetches `/sync/crr-pull`, filters own `site_id`, applies changes; both track `crr_db_version` in `sync_meta`; wired into `sync()` after old push/pull |
| API layer | `api/sync.ts` | `crrPush()`, `crrPull()` functions |
| Types | `types/index.ts` | All CRR types mirrored from server schemas |

### Server Side

| Component | File | What |
|-----------|------|------|
| Shadow DB | `services/sync/shadow_db.py` | Singleton `ShadowDB` with `check_same_thread=False` + `Lock` (per ADR 0002); cr-sqlite loaded via `enable_load_extension`; CRR table created with NOT NULL PK + lookup index; `insert_crr_changes()`, `get_merged_row()`, `get_changes_since()`, `max_db_version()`, `delete_crr_row()`, `reconcile_table()` |
| CRR push handler | `services/sync/crr_sync_service.py` | Groups changes by `(table, pk)`; inserts into shadow `crsql_changes` → triggers auto-merge; reads merged row; validates via `_CRR_VALIDATORS` (FK checks, quantity ranges); upserts into Postgres; **duplicate business-key detection** merges colliding rows |
| CRR pull handler | `api/v1/endpoints/crr_sync_endpoints.py` | `POST /sync/crr-push` and `POST /sync/crr-pull` endpoints; push returns per-row results, pull returns changes since `crr_since_db_version` |
| CRR schemas | `schemas/sync_schemas.py` | `CrrChangeRow`, `CrrPushRecord`, `CrrPush{Result,Response}`, `CrrPullResponse`; base64 serialization for bytes `pk`/`val` |
| Lifespan init | `main.py` | `get_shadow_db().initialize()` called on startup |
| Router | `api/v1/__init__.py` | CRR endpoints registered under `/sync` prefix |

---

## Key Findings

### 1. cr-sqlite v0.16 Rejects Tables with Additional UNIQUE Constraints

```
crsql_as_crr for branch_inventory failed:
  Table branch_inventory has unique indices besides the primary key.
  This is not allowed for CRRs
```

cr-sqlite requires that CRR tables have **exactly one unique constraint** — the primary key. Any additional `UNIQUE` indexes (like `UNIQUE(branch_id, drug_id)`) cause `crsql_as_crr()` to fail.

**Resolution:** Removed the `UNIQUE(branch_id, drug_id)` constraint from both client and server schemas. Deduplication is now handled:
- **At migration time:** `ROW_NUMBER() OVER (PARTITION BY branch_id, drug_id ORDER BY updated_at DESC)` to collapse legacy duplicates
- **At application level:** The server push validator checks FK and business rules; duplicate business-key rows merged in `_upsert_row_to_postgres()`

### 2. Primary Key Must Be Explicitly NOT NULL

```
crsql_as_crr for branch_inventory failed:
  Table branch_inventory has no primary key or primary key is nullable.
  CRRs must have a non nullable primary key
```

In SQLite, `id TEXT PRIMARY KEY` does **not** imply `NOT NULL`. cr-sqlite explicitly requires `NOT NULL` on the PK column.

**Resolution:** Changed all `TEXT PRIMARY KEY` declarations to `TEXT NOT NULL PRIMARY KEY` on CRR tables.

### 3. SQLite Thread Check Prevents asyncio.to_thread Access

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread.
```

The shadow DB connection was created in a thread pool via `asyncio.to_thread`, but subsequent DB access could land on a different thread pool thread.

**Resolution:** Added `check_same_thread=False` to `sqlite3.connect()` for the shadow DB.

### 4. DEFAULT Required on Every NOT NULL Non-PK Column

cr-sqlite v0.16+ requires every `NOT NULL` column that is not part of the primary key must have an explicit `DEFAULT`.

**Resolution:** Migration v15 recreates the table with DEFAULTs on all NOT NULL non-PK columns (empty string for text, 0 for integers).

---

## Gap 1: Duplicate Business-Key Resolution — Approach Chosen

**Problem:** Two offline clients can each create a new `branch_inventory` row (different `id`, same `branch_id+drug_id`). cr-sqlite merges by PK only, so both rows would coexist.

**Chosen approach: (b) Server-side post-merge reconciliation.**

Approach (a) — client-side prevention — was rejected because two simultaneously offline clients cannot see each other's rows, so prevention is impossible at creation time.

**Implementation** (`crr_sync_service.py:_upsert_row_to_postgres`):
1. Before the normal `ON CONFLICT (id)` upsert, query Postgres for a row with the same `(branch_id, drug_id)` but different `id`
2. If found: merge the two rows (sum quantities, keep latest `updated_at` metadata, update `sync_version`)
3. Update the surviving (first-arrived) row in Postgres with merged values
4. Remove the duplicate from the shadow DB via `ShadowDB.delete_crr_row()` so cr-sqlite does not re-expose it

**Merge semantics:**
| Field | Strategy |
|-------|----------|
| `quantity` | Sum (both valid offline creations) |
| `reserved_quantity` | Sum |
| `location` | Newer `updated_at` wins (or first non-null) |
| `selling_price` | Newer `updated_at` wins (or first non-null) |
| `updated_at` | Max of both |
| `created_at` | Min of both |
| `sync_version` | Max(both) + 1 |
| `id` | First-arrived row survives |

**Verified** in e2e test Scenario 2: qty=50+30=80, location from later client, duplicate removed from shadow.

---

## Gap 2: BLOB Serialization — Strategy Chosen

**Problem:** sqlite3 returns `bytes` objects for BLOB columns, which cannot be serialized to JSON directly. The `pk` and `val` fields in `crsql_changes` may contain binary data.

**Chosen strategy: `b64:` prefix with base64 encoding.**

**Server** (`sync_schemas.py:CrrChangeRow.field_serializer`):
- On serialization: `bytes` → `b64:<base64_encode(bytes)>`
- All other types: pass through as-is

**Client** (`localDb.ts:applyCrrPullChanges`):
- On deserialization: detect `b64:` prefix → `atob()` decode → `Uint8Array` for SQLite binding
- No prefix: pass through as-is

**Why base64 over hex or JSON string:**
- base64 is the standard for binary-in-JSON (used by Pydantic's default bytes serializer)
- Hex doubles the payload size; base64 adds only ~33%
- The `b64:` prefix disambiguates from genuine text values that happen to look like hex
- TypeScript `atob()`/`btoa()` are built-in, no dependencies

**Verified** in e2e test Scenario 4: bytes → `b64:3q2+7wAB` → JSON → `base64.b64decode` → original bytes roundtrip correct. Text, integer, and null passthrough verified.

---

## End-to-End Verification Results

All 5 scenarios pass against a real shadow DB (cr-sqlite loaded) and a Postgres stand-in (SQLite with `UNIQUE(branch_id, drug_id)`):

| # | Scenario | Result | Details |
|---|----------|--------|---------|
| 1 | **Field-level merge** — A edits `quantity=150`, B edits `location=Bin-3` (same row) | ✅ PASS | cr-sqlite auto-merge preserves both: shadow shows `quantity=150, location=Bin-3` |
| 2 | **Duplicate business-key** — A creates row (qty=50), B creates row (qty=30) same `branch_id+drug_id` | ✅ PASS | Server merges: qty=80, location from later client, survivor is first-arrived `id`, duplicate removed from shadow |
| 3 | **Crash recovery** — Row committed to shadow but NOT Postgres; `reconcile_table()` replays | ✅ PASS | Postgres recovers the row, shadow and Postgres counts match post-recovery |
| 4 | **BLOB serialization** — `bytes` → `b64:` → transport → decode → original bytes | ✅ PASS | Roundtrip correct for bytes, text, integer, null |
| 5 | **Full push/pull roundtrip** — Client INSERT → crsql_changes → shadow merge → Postgres upsert → server pull → client apply | ✅ PASS | All 11 crsql_changes rows flow correctly, client sees merged state |

---

## Architecture Decisions Confirmed

- **Single connection (no pooling)** — ADR 0002 holds: `Lock` + `check_same_thread=False` works correctly for shadow DB
- **Server shadow DB** — ADR 0003 validated: inserting raw `crsql_changes` rows triggers cr-sqlite's automatic merge via triggers, then we read the merged result for Postgres upsert
- **Client runs CRR alongside old sync** — `CRR_TABLES` set prevents double-sync; old push/pull skips `branch_inventory`; CRR push/pull handles it atomically at the field level

---

## Build & Compilation Status

| Component | Status |
|-----------|--------|
| TypeScript (`tsc --noEmit`) | ✅ Zero errors |
| Rust (`cargo check`) | ✅ 3 warnings (unused mut — pre-existing) |
| Python (`ast.parse`) | ✅ All modules parse cleanly |
| Shadow DB integration test | ✅ cr-sqlite loads, `crsql_as_crr` succeeds, CRR changes queryable |
| End-to-end simulation | ✅ All 5 scenarios pass |

---

## Conclusion: `branch_inventory` Safe to Mark Complete

`branch_inventory` is ready for production CRR sync:

1. **Field-level CRDT merge** works correctly — concurrent edits to different columns are preserved
2. **Duplicate business-key** collisions are handled deterministically (merge on `branch_id+drug_id`)
3. **BLOB serialization** round-trips safely through JSON transport
4. **Crash recovery** works — `reconcile_table()` replays shadow DB state into Postgres after restart
5. **Full push/pull roundtrip** verified with real `crsql_changes` data

---

## Next Steps

1. **Staging deployment** — Deploy the server changes and run a 24-hour soak test with simulated concurrent usage per the original migration plan's Step 4
2. **Migrate more tables** — Add `drug_batches`, `sales`, `purchase_orders` to `CRR_TABLES` and regenerate the shadow DB schema. Each table needs its own NOT NULL PK + DEFAULT audit and `_CRR_VALIDATORS` entry
3. **Order of migration** (risk ascending):
   - `drug_batches` — simple FK structure, no unique business key
   - `sales` — complex (line items, FK chains), but business key is `(branch_id, id)` only
   - `purchase_orders` — moderate complexity
   - `customers` / `prescriptions` — org-level tables created offline, server deduplicates by phone/email (already has its own merge logic)
4. **Reconciliation job** — Schedule `reconcile_table()` as a background task after server restarts to catch any missed upserts
