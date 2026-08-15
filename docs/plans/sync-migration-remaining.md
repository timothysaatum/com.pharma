# Sync Migration — Remaining Work

**Goal:** `grep -r "crsql\|sync_queue\|syncApi\.push\b\|syncApi\.pull\b\|syncApi\.crr" ui.laso/src/lib ui.laso/src/api backend.laso/app` returns zero hits in production code. All CI green.

**As of:** 2026-08-15

---

## What is done

- Phases 0, 1, 3 complete.
- Phase 2 complete for: sales, voids, prescriptions, customers, stock adjustments, stock transfers.
- Phase 4 partial: shadow_db / crr_sync_service / crr_sync_endpoints deleted; crsql_changes / crsql_pack_columns tables dropped client-side (localDb v26).

---

## What remains

### A — Migrate `drug_batches` to event outbox

**Why still on the queue:** `writeLocal.drugBatch` calls `upsertAndEnqueue("drug_batches", ...)` at `localWrite.ts:675`. No outbox envelope builder exists for it.

**Server side:** `DrugBatchProjector` does not exist. The `drug.py` projector handles `drug_created`/`drug_updated` (catalog). Drug *batch* (stock receipt) is a different aggregate — it affects `drug_batches` and `branch_inventory.quantity`.

#### Tasks

**A1 — Define event types**

Add to `EventEnvelope.event_type` union (`ui.laso/src/types/index.ts`) and server-side literal union (`backend.laso/app/schemas/sync_schemas.py`):
- `drug_batch_created`
- `drug_batch_updated`

Payload shape (mirrors existing `DrugBatch` model, minus computed fields):
```ts
{
  id: string;           // client ULID
  drug_id: string;
  branch_id: string;
  org_id: string;
  batch_number: string;
  quantity: number;
  remaining_quantity: number;
  cost_price: number;
  selling_price?: number;
  expiry_date?: string; // ISO date
  received_date: string;
  supplier?: string;
  purchase_order_id?: string;
  notes?: string;
}
```

**A2 — Client: add `buildDrugBatchEnvelope` to `localWrite.ts`**

Follow the pattern of `buildSaleCreatedEnvelope` (line 231). Wire into `writeLocal.drugBatch` (lines 670–682): replace `upsertAndEnqueue("drug_batches", ...)` at line 675 with `appendOutboxEvent(...)` + `upsertLocal(...)`.

Delete the `enqueue("branch_inventory", ...)` call at line 796 — it is a redundant snapshot push of a quantity already captured by `stock_adjusted` / `drug_batch_created`.

**A3 — Client: add projector to `localProjectors.ts`**

Add `case "drug_batch_created":` and `case "drug_batch_updated":` to the `applyEventLocally` switch (line 30). Each projector upserts into local `drug_batches` and updates `branch_inventory.quantity` by the delta.

**A4 — Server: add `DrugBatchProjector` to `projectors/`**

Create `backend.laso/app/services/sync/eventlog/projectors/drug_batch.py`.

- `validate`: check `drug_id`, `branch_id`, `org_id` exist; `remaining_quantity >= 0`.
- `apply` for `drug_batch_created`: INSERT INTO `drug_batches`; UPDATE `branch_inventory SET quantity = quantity + payload.remaining_quantity`.
- `apply` for `drug_batch_updated`: UPDATE `drug_batches`; reconcile `branch_inventory` delta from old vs new `remaining_quantity`.
- Register via `@ProjectorRegistry.register`.

**A5 — Server: integration test**

Add `backend.laso/tests/integration/test_drug_batch_projector.py`. Cover: create batch, update batch, duplicate `event_id` idempotency, missing `drug_id` rejection.

**Exit criterion for A:** `grep -r "upsertAndEnqueue.*drug_batch\|enqueue.*drug_batch" ui.laso/src` returns zero hits.

---

### B — Migrate `branch_inventory` metadata to event outbox

**Why still on the queue:** `writeLocal.branchDrug` calls `upsertAndEnqueue("branch_inventory", ...)` at `localWrite.ts:700`. `writeLocal.inventory` calls `enqueue("branch_inventory", ...)` at line 796 for quantity deltas.

Note: quantity deltas (sales, adjustments, batch receipts) already flow through `sale_created`, `stock_adjusted`, `drug_batch_created` events. The queue entry at line 796 is redundant — delete it outright (covered by task A2).

The metadata case (line 700, `branchDrug`) is a genuine create/update of shelf location, branch selling price, and existence flag.

#### Tasks

**B1 — Define event types**

- `branch_inventory_created` — first time a drug is linked to a branch
- `branch_inventory_updated` — metadata update (selling price, shelf location, active flag)

Payload:
```ts
{
  id: string;
  branch_id: string;
  drug_id: string;
  org_id: string;
  branch_selling_price?: number;
  shelf_location?: string;
  is_active: boolean;
  reorder_level?: number;
}
```

Quantity is NOT in this payload. Quantity is managed by sale/stock/batch events only.

**B2 — Client: add `buildBranchInventoryEnvelope` to `localWrite.ts`**

Replace `upsertAndEnqueue` at line 700 with `appendOutboxEvent(...)` + `upsertLocal(...)`.

**B3 — Client: projector in `localProjectors.ts`**

Add `branch_inventory_created` and `branch_inventory_updated` cases. Projector upserts metadata columns only; does not touch `quantity`.

**B4 — Server: add `BranchInventoryProjector` to `projectors/`**

Create `backend.laso/app/services/sync/eventlog/projectors/branch_inventory.py`.

- `validate`: `branch_id` and `drug_id` exist, belong to same org.
- `apply` for `branch_inventory_created`: INSERT OR DO NOTHING (idempotent); do not set quantity.
- `apply` for `branch_inventory_updated`: UPDATE metadata columns only; guard against overwriting quantity.

**B5 — Server: integration test**

Add `backend.laso/tests/integration/test_branch_inventory_projector.py`. Cover: create link, update price, duplicate idempotency, cross-org rejection.

**Exit criterion for B:** `grep -r "upsertAndEnqueue.*branch_inv\|enqueue.*branch_inv" ui.laso/src` returns zero hits.

---

### C — Migrate `purchase_orders` to event outbox

**Why still on the queue:** `writeLocal.purchaseOrder` calls `upsertAndEnqueue("purchase_orders", ...)` at `localWrite.ts:847`. No purchase order projector exists server-side.

Purchase orders are append-only from the branch (create + receive). They do not affect inventory directly on creation — inventory is updated when goods are received via `stock_adjusted` or `drug_batch_created`.

#### Tasks

**C1 — Define event types**

- `purchase_order_created`
- `purchase_order_updated` — status changes (submitted, received, cancelled)

Payload:
```ts
{
  id: string;
  branch_id: string;
  org_id: string;
  supplier_name?: string;
  status: "draft" | "submitted" | "received" | "cancelled";
  ordered_at?: string;
  received_at?: string;
  notes?: string;
  items: Array<{
    drug_id: string;
    drug_name: string;
    quantity_ordered: number;
    quantity_received?: number;
    unit_cost?: number;
  }>;
}
```

**C2 — Client: add `buildPurchaseOrderEnvelope` to `localWrite.ts`**

Replace `upsertAndEnqueue` at line 847 with `appendOutboxEvent(...)` + `upsertLocal(...)`.

**C3 — Client: projector in `localProjectors.ts`**

Add `purchase_order_created` and `purchase_order_updated` cases. Upsert into local `purchase_orders`. Items stored as `items_json` (existing schema).

**C4 — Server: add `PurchaseOrderProjector` to `projectors/`**

Create `backend.laso/app/services/sync/eventlog/projectors/purchase_order.py`.

- `validate`: `branch_id`, `org_id` exist; status is a valid enum value.
- `apply`: upsert `purchase_orders`; upsert `purchase_order_items` from `items` array.
- No inventory mutation — handled by stock/batch events.

**C5 — Server: integration test**

Add `backend.laso/tests/integration/test_purchase_order_projector.py`. Cover: create, status update, receive, duplicate idempotency, cross-org rejection.

**Exit criterion for C:** `grep -r "upsertAndEnqueue.*purchase_order\|enqueue.*purchase_order" ui.laso/src` returns zero hits.

---

### D — Replace `crsql_site_id()` with a stored UUID

**The issue:** `localDb.ts:2749–2759` calls `SELECT crsql_site_id()` to get the device identity used in sync. `db.rs:529` reads it after extension load. Once the extension is gone, these calls will fail.

#### Tasks

**D1 — `localDb.ts`**

In the `openDb` / init path, after creating `sync_meta`, seed a device UUID on first run:
```ts
const existing = await db.select<{ value: string }[]>(
  "SELECT value FROM sync_meta WHERE key = 'device_id' LIMIT 1"
);
if (!existing.length) {
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES('device_id', $1)",
    [crypto.randomUUID()]
  );
}
```

Replace `getCrrSiteId()` (line 2749) with:
```ts
let _device_id: string | null = null;

export async function getDeviceId(): Promise<string> {
  if (_device_id) return _device_id;
  const db = await getDb();
  const rows = await db.select<{ value: string }[]>(
    "SELECT value FROM sync_meta WHERE key = 'device_id' LIMIT 1"
  );
  _device_id = rows[0]?.value ?? "";
  return _device_id;
}
```

Update all callers of `getCrrSiteId()` to `getDeviceId()`.
Find them: `grep -n "getCrrSiteId\|_crr_site_id" ui.laso/src`.

**D2 — `db.rs`**

Remove the `crsql_site_id()` query at line 529 (used only for startup logging). Device ID is now owned by TypeScript / `sync_meta`.

**Exit criterion for D:** `grep -rn "crsql_site_id\|getCrrSiteId\|_crr_site_id" ui.laso/src` returns zero hits.

---

### E — Strip cr-sqlite from Rust (`db.rs`)

Do this after A, B, C, D are complete.

#### Tasks

**E1 — Remove extension loading from `db.rs`**

Delete:
- `fn crsqlite_library_name()` (line 46–53)
- `fn crsqlite_platform_dir()` (line 56–~90)
- The entire "find and load crsqlite" block inside the connection open function (lines 95–129)
- The `crsql_site_id()` query at line 529
- The `crsqlite_platform_dir` import in the test module (line 546)

The connection open function should open the SQLCipher database and set pragmas only — no extension loading.

**E2 — Delete `tests/crsqlite_compat.rs`**

`ui.laso/src-tauri/tests/crsqlite_compat.rs` — delete entirely.

**E3 — Remove `load_extension` Cargo feature if added for crsqlite**

Check: `grep -n "load_extension\|loadable_extension" ui.laso/src-tauri/Cargo.toml`.

If the feature was added exclusively for cr-sqlite, remove it. If it is needed by SQLCipher itself, leave it.

**E4 — Delete `spike_sqlcipher_load_extension_after_pragma_key` test**

Lines 577–630 in `db.rs`. This test verifies `load_extension` works after the SQLCipher key pragma — no longer needed. Delete it.

Keep all other `db.rs` tests (encryption, WAL, etc.).

**Exit criterion for E:** `grep -rn "crsql\|load_extension\|crsqlite" ui.laso/src-tauri/src ui.laso/src-tauri/tests` returns zero hits.

---

### F — Drop `sync_queue` from client schema

Do this after A, B, C are complete.

#### Tasks

**F1 — localDb migration v27**

Add after the v26 block:
```ts
// MIGRATION V27 — drop sync_queue (all entities now on event_outbox)
if (currentVersion < 27) {
  await db.execute("DROP TABLE IF EXISTS sync_queue");
  await db.execute(
    "INSERT INTO sync_meta(key, value) VALUES('schema_version', '27') ON CONFLICT(key) DO UPDATE SET value='27'"
  );
}
```

**F2 — Delete `sync_queue` CREATE TABLE DDL from `localDb.ts`**

**F3 — Delete all `sync_queue` helper functions from `localDb.ts`**

Functions to delete: `enqueue`, `dequeue`, `markQueueError`, `markQueueConflict`, `getQueue`, `getQueueCount`, `getQueueItem`, `incrementAttempts`, `markPermanentlyRejected`, `getQueuedConflicts`, `markQueueSynced`, `markQueueVoided`, `updateQueuePayload`.

Find them: `grep -n "^export async function\|^export function" ui.laso/src/lib/localDb.ts | grep -i "queue\|enqueue\|dequeue"`.

**F4 — Delete `upsertAndEnqueue` from `localWrite.ts`**

Lines 490–537. Dead once A, B, C call sites are removed.

**F5 — Update all imports**

Remove `enqueue`, `dequeue`, `markQueueError`, `markQueueConflict` from import lines.

**Exit criterion for F:** `grep -rn "sync_queue\|enqueue\|dequeue\|markQueueError" ui.laso/src/lib` returns zero hits.

---

### G — Delete legacy sync push/pull path

Do this after F is complete.

#### Tasks

**G1 — `syncEngine.ts`**

Delete the `pushItems()` method that drains `sync_queue` via `syncApi.push` (lines ~490–620).

Delete the legacy `pullItems()` calls to `syncApi.pull` (lines 677 and 768).

The sync loop `syncOnce()` should call only `pushEvents()` and `pullEvents()`.

**G2 — `api/sync.ts`**

Delete:
- `pull` (line 36–37, `/sync/pull`)
- `push` (line 43–44, `/sync/push`)
- `crrPush` (line 55–56, `/sync/crr-push`)
- `crrPull` (line 59–60, `/sync/crr-pull`)

Keep: `voidFailedSale`, `pushEvents`, `pullEvents`, `getSyncStatus`.

**G3 — `sync_endpoints.py`**

Delete routes: `pull` (line 62), `push` (line 107), `push_async` (line 235), `push_async_status` (line 283).

Keep: `void_failed_sale` (line 147) and `sync_status` (line 214).

If `void_failed_sale` and `sync_status` are the only survivors, move them into `event_sync_endpoints.py` and delete `sync_endpoints.py` entirely. Verify router registration in the main app file before deleting.

**G4 — `sync_schemas.py`**

Remove schema classes used only by the old push/pull wire format:
- `PushRecord`, `PushRequest`, `PushResponse`
- `PullRequest`, `PullResponse` (confirm not reused by event pull)
- `CrrPushRequest`, `CrrPushResponse`
- `CrrChange`, `SyncItem` and related

Keep all schemas used by `event_sync_endpoints.py` and `void_failed_sale`.

**Exit criterion for G:** `grep -rn "syncApi\.push\b\|syncApi\.pull\b\|syncApi\.crr\|/sync/push\|/sync/pull\|/sync/crr" ui.laso/src backend.laso/app` returns zero hits.

---

### H — Delete dead tests, CI steps, and binaries

**H1 — Client test files to delete**

```
ui.laso/src/lib/__tests__/crrMetadataRepair.test.ts
ui.laso/src/lib/__tests__/crrPermanentlyRejected.test.ts
ui.laso/src/lib/__tests__/crrSaleProjectionRouting.test.ts
```

Check `customerMergeDirective.test.ts` — if it only tests the old CRR customer merge directive flow (`customer_merge_directives` table was dropped in v26), delete it.

**H2 — Server e2e test files to delete**

```
backend.laso/tests/e2e_crr_sync.py
backend.laso/tests/e2e_crr_sync_pg.py
backend.laso/tests/e2e_crr_drug_batches.py
```

These test the shadow-DB CRDT path which no longer exists.

**H3 — `ci.yml`**

Remove the "Install native cr-sqlite extension" step (lines 80–85) and the `CRSQLITE_EXTENSION_PATH` env var.

**H4 — `build-tauri.yml`**

Remove:
- Matrix variables: `crsqlite_asset`, `crsqlite_platform`, `host_crsqlite_asset`, `host_crsqlite_platform`, `test_crsqlite` (lines 45–79)
- "Install target cr-sqlite extension" step (lines 115–120)
- "Install host cr-sqlite extension for Rust tests" step (lines 122–128)
- "Test cr-sqlite compatibility" step (lines 145–148, runs `cargo test --test crsqlite_compat`)

**H5 — Delete repo artifacts**

```
crsqlite/                                         ← entire directory
ui.laso/scripts/install-crsqlite-extension.mjs
```

**H6 — Update docs**

`docs/decisions/0006-event-sourced-sync-spine.md`: change the Consequences bullet from "cr-sqlite native extension removed from required startup path (loading is now optional/warn-only)" to "cr-sqlite native extension removed entirely".

**Exit criterion for H:** `grep -rn "crsqlite\|cr-sqlite\|crsql" .github/ ui.laso/scripts/` returns zero hits. The `crsqlite/` directory does not exist.

---

## Execution order

```
A (drug_batches outbox)      ┐
B (branch_inventory outbox)  ├── parallel, independent worktrees
C (purchase_orders outbox)   ┘
D (device UUID)              ← also parallel with A/B/C
          ↓ all done
E (strip db.rs)
F (drop sync_queue)
          ↓
G (delete legacy push/pull)
          ↓
H (CI, test files, binaries)
```

---

## Final verification

All must return zero:

```bash
grep -rn "crsql" ui.laso/src backend.laso/app
grep -rn "sync_queue" ui.laso/src backend.laso/app
grep -rn "syncApi\.push\b\|syncApi\.pull\b\|syncApi\.crr" ui.laso/src
grep -rn "upsertAndEnqueue\|enqueue(" ui.laso/src/lib
grep -rn "shadow_db\|crr_sync" backend.laso/app
grep -rn "crsqlite" .github/ ui.laso/scripts/ ui.laso/src-tauri/src ui.laso/src-tauri/tests
```
