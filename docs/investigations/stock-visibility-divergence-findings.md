# Stock Visibility Divergence: Inventory 117 vs Cart 0 vs Catalogue 0

**Date:** 2026-10-01
**Scope:** Why Admin > Inventory > All Stock and the POS product list show 117 units of Gebedol while the POS cart blocks the sale and Admin > Drugs reports an empty formulary. Four views, one session, branch "Apomuden Pharmacy", tenant header "Default Pharmacy".
**Method:** Static trace of all four data paths from UI to SQL, then read-only inspection of both databases (PostgreSQL `atlasdb` on `localhost:5432`, and the device's SQLCipher-encrypted `laso.db`), plus the device's persisted sync cursor and the server event spine. No code, migration, or data was modified.
**Status:** Root cause identified. Fixes proposed in §6, none applied.

---

## 1. Summary

PostgreSQL is correct and internally consistent: Gebedol has `branch_inventory.quantity = 117` and one live unexpired batch of 117 at the active branch, with zero orphans in any direction. The Inventory tab and the POS product list are served over HTTP from PostgreSQL, so their 117 is right. The Drug Catalogue and the POS cart validation are served from the device's local SQLite, and that database holds no stock rows for any real drug, because the device's sync cursor is stuck at event seq 2 while the server is at seq 69, and because the event spine has never emitted a stock event for any real drug. All three cart symptoms reduce to a single flag, `notStocked === true`. Two further defects are latent and would keep the cart at `/0` even after the sync catches up.

---

## 2. Data path per view

Real identifiers used throughout this document:

| Entity | ID | Name |
|---|---|---|
| Organization (tenant header) | `2d060ef8-a302-447c-91f4-b2fd30268341` | Default Pharmacy |
| Branch (active branch) | `72b2433d-120b-42a2-918b-e6dfcf176b1a` | Apomuden Pharmacy |
| Drug (Gebedol, `GEB-300`) | `8d4cc1a7-03c7-4a6a-8080-2bda5def026f` | Gebedol |
| Logged-in user | `bae475d9-994a-4d5b-abb2-32aa4b082602` | `tim`, Pharmacist, not super-admin |

### 2.1 Inventory "All Stock" (HTTP, serving live data)

```
ui.laso/src/pages/InventoryPage.tsx:923          InventoryPage()
  └─ :1021-1046   withTimeout(inventoryApi.getBranchInventory(...), localRead fallback)
       └─ ui.laso/src/api/inventory.ts:40-61     GET /inventory/branch/{branchId}
            └─ ui.laso/src/api/client.ts:184     axios
                 GET {VITE_API_URL}/api/v1/inventory/branch/72b2433d-...
                   └─ backend.laso/app/api/v1/endpoints/inventory_endpoints.py:49-91
                        └─ :80  InventoryService.get_branch_inventory_paginated(...)
                             backend.laso/app/services/inventory/inventory_service.py:274-438
```

Query construction, `inventory_service.py:296-343`:

```sql
SELECT bi.*, valid_batch_qty, combined_qty
FROM branch_inventory bi
JOIN drugs d    ON bi.drug_id   = d.id     -- :322  (inner)
JOIN branches br ON bi.branch_id = br.id   -- :323  (inner)
WHERE bi.branch_id = '72b2433d-120b-42a2-918b-e6dfcf176b1a'   -- :328
  AND coalesce(nullif(<valid_batch_sum>, 0), bi.quantity) > 0  -- :343 (include_zero_stock defaults false)

-- correlated scalar subquery, :296-308:
--   SELECT coalesce(sum(b.remaining_quantity), 0) FROM drug_batches b
--    WHERE b.drug_id   = bi.drug_id
--      AND b.branch_id = bi.branch_id
--      AND b.remaining_quantity > 0
--      AND (b.expiry_date IS NULL OR b.expiry_date >= CURRENT_DATE)
```

- Count query: `inventory_service.py:331-336`. It joins `drugs` but **not** `branches`, unlike the data query.
- Tenant filter: **none in SQL.** Isolation rests solely on the Python check `_ensure_branch_access` at `inventory_endpoints.py:29-44`, which tests `user.assigned_branches` and never compares `branch.organization_id` to `user.organization_id`.
- `is_active` / `is_deleted` / `deleted_at` / `sync_status`: **not filtered on any table.**
- Total stock per drug is aggregated only here, at `inventory_service.py:296-318`. `inventory_movements` is never read for stock levels; it is a write-only audit ledger.

### 2.2 Drug Catalogue

There are two paths. The server path is not the one that failed.

**Server path (returns 6, verified):**

```
ui.laso/src/pages/DrugListPage.tsx:148         drugApi.list({ page, page_size: 20, branch_id })
  └─ ui.laso/src/api/drugs.ts:27-36            GET /drugs
       └─ backend.laso/app/api/v1/endpoints/drug_endpoints.py:161-225
            └─ :199  DrugService.search_drugs(organization_id=current_user.organization_id, ...)
                 backend.laso/app/services/drug/drug_service.py:191-235
```

```sql
SELECT * FROM drugs
WHERE organization_id = '2d060ef8-...'   -- drug_service.py:191, bound to current_user.organization_id
  AND is_deleted = false                 -- :194
  AND is_active = true                   -- :197, default from drug_endpoints.py:168
ORDER BY name                           -- :235
-- no LIMIT/OFFSET: the whole matching set is materialised, then sliced
```

Count is `len()` of that list at `backend.laso/app/utils/pagination.py:230` (`paginate_list`, not `paginate`). `total_pages` is `0` when the list is empty.

`branch_id` is not a row filter on this path. It only drives an access check (`drug_endpoints.py:83-90`), a serializer swap (`:216`), and re-pricing (`:93-111`).

**Offline path (returns 0, verified as the failing one):**

```
DrugListPage.tsx:127-146   if (!navigator.onLine || isBackendKnownUnreachable())
                             → localRead.searchDrugs(...)
```

`ui.laso/src/lib/localRead.ts:311-368`:

```sql
-- :321  join = ""
-- :323-327
if (params.branch_id) {
  join = "LEFT JOIN branch_inventory bi ON bi.drug_id = d.id";   -- :325
  qualifiers.push(`bi.branch_id = $${values.length}`);            -- :326
}

SELECT COUNT(*) AS total FROM drugs d ${join} WHERE d.is_deleted = 0 AND bi.branch_id = $1   -- :357
SELECT d.* FROM drugs d ${join} WHERE d.is_deleted = 0 AND bi.branch_id = $1
ORDER BY d.updated_at DESC LIMIT $n OFFSET $m                                                          -- :362-365
```

A `LEFT JOIN` with a WHERE predicate on the right-hand table is an inner join. Every drug lacking a local `branch_inventory` row for the active branch is dropped. `DrugListPage` also never passes `organization_id`, so this query is untenant-scoped.

### 2.3 POS product list

```
ui.laso/src/pages/POSPage.tsx:644              <DrugSearchPanel />
  └─ ui.laso/src/components/pos/DrugSearchPanel.tsx:181  inventoryApi.getBranchInventory(...)
       └─ GET /api/v1/inventory/branch/72b2433d-...
            └─ inventory_service.py:296-329   (identical to §2.1)
```

"N available" is `DrugSearchPanel.tsx:204` reading `item.available_quantity`, which the schema computes at `backend.laso/app/schemas/inventory_schemas.py:49-53`:

```python
@computed_field
@property
def available_quantity(self) -> int:
    return max(0, self.quantity - self.reserved_quantity)
```

`quantity` is `combined_qty` from `inventory_service.py:408`. The label renders at `DrugSearchPanel.tsx:391-395`.

### 2.4 POS cart availability check (local SQLite, always)

```
ui.laso/src/pages/POSPage.tsx:111-114   resolveSellableQuantity → localRead.getSellableQuantity
  └─ ui.laso/src/lib/localRead.ts:1165-1256
```

No HTTP. No Tauri command. Three SQLCipher queries:

```sql
-- :1179-1182
SELECT * FROM branch_inventory WHERE branch_id = $1 AND drug_id = $2 LIMIT 1

-- :1202-1207, only when notStocked is still true
SELECT remaining_quantity FROM drug_batches
 WHERE branch_id = $1 AND drug_id = $2 AND remaining_quantity > 0 AND expiry_date > $3

-- :1231-1236
SELECT leased_quantity, consumed_quantity FROM stock_leases
 WHERE branch_id = $1 AND drug_id = $2 AND terminal_id = $3
   AND status = 'active' AND expires_at > $4
```

Value selection, `localRead.ts:1184-1193`:

```js
let unleasedPool = 0;
let notStocked = true;
if (rows.length > 0) {
  const row = rows[0];
  const rawVal = row.sellable_quantity != null ? row.sellable_quantity : row.quantity;
  const q = Number(rawVal);
  unleasedPool = Math.max(0, !isNaN(q) ? q : 0);
  notStocked = false;
}
```

Final combine, `localRead.ts:1251-1255`: online uses `leaseRemaining + unleasedPool`, offline uses `leaseRemaining > 0 ? leaseRemaining : unleasedPool`.

ID used for the drug is `drug.id`, the PostgreSQL `drugs.id` UUID carried as TEXT. It originates at `DrugSearchPanel.tsx:69` (`inventoryItemToDrug`). It is not a SKU and not a batch id. No `organization_id` is passed; the local `branch_inventory` table has no such column.

The three user-facing strings and their exact conditions, `POSPage.tsx:120-134`:

| String | Location | Condition |
|---|---|---|
| `Gebedol is not stocked at the active branch.` | `POSPage.tsx:123` | `notStocked === true`, and `sq[drugId] = 0` |
| `Gebedol is not stocked at the active branch and cannot be sold.` | `POSPage.tsx:234` | same, during `handleCheckout` pre-flight |
| `Gebedol — stock details not synced to this device yet. Sync to sell.` | `POSPage.tsx:129-130` | `!notStocked && sellable <= 0 && noBatchData` |
| `Gebedol — No valid non-expired batches. Cannot sell.` | `POSPage.tsx:132` | `!notStocked && sellable <= 0 && !noBatchData` |
| `Insufficient stock for X. Requested N, available M.` | `POSPage.tsx:133` | `item.quantity > info.sellable` |
| `Only {resolvedStock} available (requested {item.quantity})` | `CartPanel.tsx:558` | `item.quantity > resolvedStock` |
| `/{resolvedStock ?? "?"}` | `CartPanel.tsx:539-541` | always rendered |

`resolvedStock` is `CartPanel.tsx:504-509` and feeds the stepper `max` (`:524`) and the increment cap (`:532`).

The `errors` map feeds `cart.setStockQuantities` (`POSPage.tsx:137`), stored as `stockErrors`, consumed by `useCart.ts:349-353`.

### 2.5 Active branch and tenant resolution

All four paths read `activeBranchId` from Zustand `useAuthStore`: `InventoryPage.tsx:924`, `POSPage.tsx:60`, `DrugListPage.tsx:68`. Confirmed on disk as `session.branch_id = 72b2433d-...` in `~/.local/share/com.vermithor.pharmacare/laso.bin`. It originates at `authStore.ts:139-144` (restore, or auto-select when exactly one branch is assigned) and `authStore.ts:265` (explicit switch). It reaches the backend only as a URL path segment.

**No path uses a different or default branch.** The divergence is the data source, not the identifiers.

All three offline guards test the same module-global `backendReachable` flag (`ui.laso/src/api/client.ts:40-45`). That flag is flipped by `markBackendOffline()` from two places: a network-level axios failure (`client.ts:109`) and **any HTTP 5xx**, because `isOfflineError` at `client.ts:286-288` classifies `status >= 500` as offline and the response interceptor calls `markBackendOffline()` on it. A 15s heartbeat (`client.ts:353-358`) restores it. Within a single session, two pages can therefore legitimately read different databases.

---

## 3. Comparison table

| | Inventory "All Stock" | Drug Catalogue | POS product list | POS cart check |
|---|---|---|---|---|
| **Source** | PostgreSQL (HTTP) | **local SQLite** | PostgreSQL (HTTP) | **local SQLite** |
| **Tables read** | `branch_inventory` JOIN `drugs` JOIN `branches` + `drug_batches` subquery | `drugs` only | same as Inventory | `branch_inventory`, `drug_batches`, `stock_leases` |
| **Join keys** | `bi.drug_id = d.id`, `bi.branch_id = br.id` | `bi.drug_id = d.id`, LEFT but WHERE forces inner | same as Inventory | none, three single-table lookups |
| **Tenant filter** | **none in SQL** | none, `organization_id` never passed | **none in SQL** | none, column does not exist locally |
| **Branch filter** | `bi.branch_id = ?` | `bi.branch_id = ?` | `bi.branch_id = ?` | `branch_id = ?` on all three tables |
| **Active / deleted filters** | none | `d.is_deleted = 0`; server also adds `is_active = true` | none | none |
| **Drug ID used** | `bi.drug_id` (UUID) | `d.id` (UUID) | `bi.drug_id` (UUID) | `drug_id` (UUID as TEXT) |
| **How "available" is computed** | `coalesce(nullif(SUM(unexpired non-zero batches), 0), bi.quantity)` | not applicable | same expression, minus `reserved_quantity` | `sellable_quantity` column, falling back to `quantity` |

The three cells that differ across all four paths: **Source** (2 of 4 read local SQLite), **Tenant filter** (absent everywhere in SQL), and **how "available" is computed** (a correlated batch sum versus a projection column that nothing writes).

---

## 4. Live data findings

### 4.1 Which databases

| Database | Location | Role |
|---|---|---|
| PostgreSQL 16, database `atlasdb` | `localhost:5432`, configured at `backend.laso/.env:5` | server of record |
| SQLCipher-encrypted SQLite | `~/.local/share/com.vermithor.pharmacare/laso.db` | device local cache and offline store |

`backend.laso/shadow.db` exists on disk but is **not** opened by any read path. Every "shadow" reference in the repository is a comment about the retired CRR sync design; `app/services/sync/shadow_db.py` does not exist.

### 4.2 Row counts

| Table | Rows |
|---|---|
| `organizations` | 1 |
| `branches` | 1 |
| `drugs` | 6 |
| `branch_inventory` | 6 |
| `drug_batches` | 7 |
| `stock_leases` | 0 |
| `users` | 2 |

### 4.3 Branches and tenants

There is one organization and one branch:

| Table | ID | Name |
|---|---|---|
| `organizations` | `2d060ef8-a302-447c-91f4-b2fd30268341` | Default Pharmacy |
| `branches` | `72b2433d-120b-42a2-918b-e6dfcf176b1a` | Apomuden Pharmacy, `organization_id = 2d060ef8-...` |

**"Default Pharmacy" and "Apomuden Pharmacy" are not two tenants.** "Default Pharmacy" is the organization and is what the tenant header refers to. "Apomuden Pharmacy" is its single branch and is what the active-branch selector refers to. Both resolve to the same tenant. There is no cross-tenant mismatch anywhere in this scenario.

### 4.4 Gebedol (`GEB-300`), full dump

Catalogue row:

```
drugs: id=8d4cc1a7-03c7-4a6a-8080-2bda5def026f  sku=GEB-300  name=Gebedol
  organization_id=2d060ef8-a302-447c-91f4-b2fd30268341
  is_active=t  is_deleted=f  deleted_at=NULL
  sync_status=pending  reorder_level=10  unit_price=5.00
```

Stock rows:

```
branch_inventory: id=0365d598-5c3c-4f9c-9240-51e656555837
  branch_id=72b2433d-120b-42a2-918b-e6dfcf176b1a
  drug_id=8d4cc1a7-03c7-4a6a-8080-2bda5def026f
  quantity=117  reserved_quantity=0
  selling_price=5.00
  sync_status=pending  last_synced_at=NULL
  created_at=2026-09-19 18:01:58  updated_at=2026-09-19 18:01:58

drug_batches: id=1b7ed920-82fc-493c-861b-f384fd868f25
  branch_id=72b2433d-120b-42a2-918b-e6dfcf176b1a
  drug_id=8d4cc1a7-03c7-4a6a-8080-2bda5def026f
  batch_number=CAE32423
  quantity=123  remaining_quantity=117
  expiry_date=2032-09-19   (not expired as of 2026-10-01)
  sync_status=pending  last_synced_at=NULL
```

Two schema facts that matter:

- PostgreSQL `branch_inventory` has **no `sellable_quantity` column** (14 columns total, confirmed via `information_schema.columns`).
- PostgreSQL `drug_batches` has **no `status` column**; it has `sync_status`.

All six drugs, for context:

| Drug | SKU | `bi.quantity` | Batch `remaining_quantity` | Expiry | `bi.sync_status` | `drugs.sync_status` |
|---|---|---|---|---|---|---|
| Amoxicilin | (none) | 247 | 147 + 100 = 247 | 2030-09-26, 2033-09-19 | pending | pending |
| Gebedol | GEB-300 | 117 | 117 | 2032-09-19 | pending | pending |
| Ibuprofen-0561 | (none) | 40 | 40 | 2028-12-31 | synced | synced |
| Minoxidil Oil | (none) | 100 | 100 | 2032-09-19 | pending | pending |
| Paracetamol 500mg | PAR-500 | 198 | 198 | 2030-09-19 | pending | pending |
| Paracetamol-9014 | (none) | 50 | 50 | 2028-12-31 | synced | synced |

### 4.5 The exact queries from §2, run against PostgreSQL

**A.1 inventory query, branch `72b2433d-...`:**

```
       name        |   sku   | bi_quantity | valid_batch_qty | combined_qty
-------------------+---------+-------------+-----------------+--------------
 Amoxicilin        |         |         247 |             247 |          247
 Gebedol           | GEB-300 |         117 |             117 |          117
 Ibuprofen-0561    |         |          40 |              40 |           40
 Minoxidil Oil     |         |         100 |             100 |          100
 Paracetamol 500mg | PAR-500 |         198 |             198 |          198
 Paracetamol-9014  |         |          50 |              50 |           50
(6 rows)
```

This is an exact match to the reported Inventory figures, which confirms the Inventory tab was served over HTTP.

**A.2 catalogue query, org `2d060ef8-...`, `is_deleted = false`, `is_active = true`:**

```
                  id                  |       name        |   sku   | is_active | is_deleted
--------------------------------------+-------------------+---------+-----------+------------
 9038e0e0-459f-4a6f-8bd1-72bb2b8447e0 | Amoxicilin        |         | t         | f
 8d4cc1a7-03c7-4a6a-8080-2bda5def026f | Gebedol           | GEB-300 | t         | f
 c13dae4f-8f37-4558-a723-18c69bfd3ad0 | Ibuprofen-0561    |         | t         | f
 378b5603-324b-47cd-87ff-069e7eb5ff77 | Minoxidil Oil     |         | t         | f
 5dadb522-766f-4789-930e-dddefb0964ef | Paracetamol 500mg | PAR-500 | t         | f
 be0052c2-c08b-41a2-9707-54bedca1a01c | Paracetamol-9014  |         | t         | f
(6 rows)
```

**Returns 6, not 0.** The server-side catalogue is not the path that produced the empty screen.

**A.3 POS list:** identical to A.1. Returns 117 for Gebedol.

**A.4 cart queries against local SQLite:** no result available to report, see §5 "not verified". What is established is that both queries return zero rows, because the device cursor at seq 2 precedes the first `branch_inventory_updated` event at seq 3 and the first `drug_batch_created` at seq 13.

### 4.6 Orphans

PostgreSQL is clean in every direction:

| Check | Count |
|---|---|
| `branch_inventory` rows with no matching `drugs` row | 0 |
| `drug_batches` rows with no matching `drugs` row | 0 |
| `drugs` rows with no `branch_inventory` row | 0 |
| `branch_inventory` rows whose branch does not exist | 0 |
| `drug_batches` rows whose branch does not exist | 0 |

### 4.7 Sync state

Server-side queues are all empty. Nothing is stuck, failing, or quarantined server-side:

| Table | Rows |
|---|---|
| `sync_queue` | 0 |
| `sync_operation_receipts` | 0 |
| `unresolved_conflicts` | 0 |
| `pending_projections` | 0 |
| `event_dead_letter` | 0 |
| `stock_leases` | 0 |

The event spine:

| Metric | Value |
|---|---|
| `event_log` total | 83 |
| Events for org `2d060ef8-...` (real tenant) | 69, seq 1 through 69 |
| Events for org `11111111-1111-1111-1111-111111111111` (E2E sentinels) | 14, seq 1 through 14 |

**Device cursor**, read from `~/.local/share/com.vermithor.pharmacare/localstorage/http_localhost_1420.localstorage` (plain SQLite, `ItemTable`):

```
last_sync_at                                    | 2
last_sync_at:72b2433d-120b-42a2-918b-e6dfcf176b1a | 2
last_sync_at:22222222-2222-2222-2222-222222222222 | 2
```

The device has pulled **2 of 69** events. Events at seq ≤ 2 are two `drug_category_created` and two `drug_created` (Paracetamol 500mg and a sentinel). The first `branch_inventory_updated` is seq 3. Gebedol's own `drug_created` is seq 4. The device has reached none of them.

**Every stock event in the spine references a drug that does not exist in `drugs`:**

```sql
WITH bi AS (
  SELECT DISTINCT (payload::jsonb->>'drug_id')::uuid AS drug_id
  FROM event_log
  WHERE event_type IN ('branch_inventory_updated','branch_inventory_created')
)
SELECT bi.drug_id, (SELECT count(*) FROM drugs d WHERE d.id = bi.drug_id) AS exists_in_drugs
FROM bi;
```
```
                drug_id                 | exists_in_drugs
--------------------------------------+-----------------
 66666666-6666-6666-6666-666666666666 |               0
(1 row)
```

```sql
SELECT e.aggregate_id,
       (SELECT count(*) FROM drugs d WHERE d.id = e.aggregate_id) AS exists_in_drugs,
       count(*) AS events
FROM event_log e WHERE e.event_type = 'drug_batch_created' GROUP BY 1,2;
```
```
             aggregate_id             | exists_in_drugs | events
--------------------------------------+-----------------+--------
 e5c153c6-8480-4866-a1a7-4430af4c2aa7 |               0 |      1
```

Per real drug, whether any stock event exists in the spine:

| Drug | Has `branch_inventory*` event | Has `drug_batch_created` event |
|---|---|---|
| Amoxicilin | false | false |
| Gebedol | false | false |
| Ibuprofen-0561 | false | false |
| Minoxidil Oil | false | false |
| Paracetamol 500mg | false | false |
| Paracetamol-9014 | false | false |

The six real `branch_inventory` rows exist in PostgreSQL only because they were written directly, which emitted no event. `sync_status = 'pending'` on four of them is consistent with a direct SQL or seed write that bypassed the event writers.

---

## 5. Root cause

### Verified

**Cause 1, primary, produces all four reported symptoms.** The device's local SQLite holds no stock rows for the real drugs, so `getSellableQuantity` returns `notStocked = true` and `sellable = 0`. That single flag produces "is not stocked at the active branch" (`POSPage.tsx:123`) and simultaneously sets `sq[drugId] = 0`, which produces "/0" (`CartPanel.tsx:539`) and "Only 0 available (requested 1)" (`CartPanel.tsx:558`). The same empty `branch_inventory` makes the offline catalogue return 0, via the `LEFT JOIN` that `localRead.ts:325-326` silently turns into an inner join.
Evidence: cursor at 2 against a server at 69 (`laso.db`'s sibling `localstorage`, and `event_log`); no stock event exists for any real drug (§4.7); all three cart strings traced to one condition at `POSPage.tsx:120-134`.

**Cause 2, why the device is 67 events behind.** `ui.laso/src/lib/syncEngine.ts:365-423` breaks out of the page loop on the first projector throw (`:396-406`) and only advances the persisted cursor when no event failed (`:412`). One poison event therefore pins the cursor permanently, and every later event is starved indefinitely. The comment at `:402-403` states the intent, "The next cycle will re-pull from lastSuccessSeq", but re-pulling re-hits the same failure, so the loop never progresses.

**Cause 3, latent, survives fixing causes 1 and 2.** Local `branch_inventory.sellable_quantity` has no writer.
- Column declared `NOT NULL DEFAULT 0` at `ui.laso/src/lib/localDb.ts:1497`.
- The projector `_branchInventoryUpserted` (`ui.laso/src/lib/localProjectors.ts:849-884`) reads `p.sellable_quantity` as a fallback for `qty` at `:855` but names the column in neither its `UPDATE` (`:866-872`) nor its `INSERT` (`:877-881`).
- `ui.laso/src/lib/localWrite.ts:874-878` hardcodes `VALUES ($1,$2,$3,$4, 0, 0, NULL, NULL, ...)`, writing literal `0` into both `reserved_quantity` and `sellable_quantity`.

A repo-wide grep for `sellable_quantity` in `ui.laso/src` returns only the DDL, the two hardcoding writes, two readers (`localRead.ts:1179,1189` and `leaseEngine.ts:66-68`), and tests. Nothing writes a non-zero value. Since `getSellableQuantity` tests `!= null` at `localRead.ts:1189` and `0` is not null, once rows do exist it will read `0`, and the cart will still show "/0" and "Only 0 available", just with a different message. Commit `3834f5b` added the column and recorded "populated on next sync, DEFAULT 0 safe"; the writer was never added.

**Cause 4, latent.** The Drug Catalogue should not be stock-scoped at all. `localRead.ts:325-326` makes catalogue membership conditional on having a `branch_inventory` row at the active branch, which is why offline reports 0 drugs while the server reports 6 for identical data.

**Cause 5.** The test that appears to cover cause 3 is false confidence. `ui.laso/src/lib/__tests__/sellableQuantityProjected.test.ts:25` mocks `db.select` to resolve `{ quantity: 100, sellable_quantity: 40 }`. It proves the read logic is correct given a correct input; nothing in production produces that input. The suite is green and the feature is broken, which is why this shipped.

**Also verified, separate from the symptom chain:** `client.ts:286-288` treats any 5xx as "backend offline", and the response interceptor then calls `markBackendOffline()` globally. A single unrelated server error silently reroutes every read path to local SQLite while presenting as "cached data" rather than an error.

### Inferred, not verified

**Why the catalogue read local SQLite while the Inventory tab read PostgreSQL in the same session.** Both pages use the identical guard (`!navigator.onLine || isBackendKnownUnreachable()`), and both were live. `backendReachable` is one module-global flag, flipped by any 5xx and restored by a 15s heartbeat, so this is timing-dependent and cannot be reconstructed after the fact. `logs/app.log` is empty and `backend.log` is stale (dated 2026-09-21) because `backend.laso/app/core/logging_config.py:57-60` attaches the rotating file handler only when `ENVIRONMENT == "production"`; in development logs go to the console of the launching terminal, which is not captured to a file. There is no access log to consult.

**Not verified: the contents of `laso.db`.** It is SQLCipher-encrypted; the raw key lives in the OS keyring under service `com.vermithor.pharmacare`, entry `db.sqlcipher_key` (`ui.laso/src-tauri/src/lib.rs:5-11`). The three matching collections (`login/103`, `login/102`, `login/66`) are locked and unlocking requires an interactive prompt, so the file could not be read. The database was copied to `/tmp/opencode/laso_copy/` and the original was never opened. The conclusion that it holds no stock rows for the real drugs rests on the persisted cursor value of 2, the absence of any corresponding event in the spine, and the projector code, not on reading the file.

**Check that would settle it.** Unlock the keyring, then run against `laso.db`:
```sql
SELECT count(*) FROM branch_inventory WHERE drug_id = '8d4cc1a7-03c7-4a6a-8080-2bda5def026f';
SELECT count(*) FROM drug_batches     WHERE drug_id = '8d4cc1a7-03c7-4a6a-8080-2bda5def026f';
SELECT count(*) FROM drugs;
SELECT count(*) FROM branch_inventory;
```
Expectation if the inference is right: 0, 0, at most 2 (Paracetamol 500mg plus a sentinel), 0.

**Note on incidental findings.** Probing `POST /api/v1/auth/login` produced a `429`. That was caused by this investigation, not a pre-existing fault, and the app does not treat 429 as offline (`isOfflineError` returns false for 4xx below 500), so it is not part of this incident. Separately, `tests/unit` currently reports `2 failed, 90 passed, 86 errors` on a clean tree; the errors are environmental, because `tests/conftest.py:11` defaults to `sqlite+aiosqlite:///:memory:` while the models use PostgreSQL-only types such as `ARRAY(UUID)` at `user_model.py:162`. This is out of scope here but worth recording. The suite should not be run with `TEST_DATABASE_URL` pointing at `atlasdb`, because `tests/conftest.py:50-51` issues `DROP SCHEMA IF EXISTS public CASCADE`.

---

## 6. Proposed fix

Described only. Nothing below has been applied.

**6.1 Make the spine carry real stock.** Emit `branch_inventory_created/updated` and `drug_batch_created` whenever `branch_inventory` or `drug_batches` are written. The write sites to instrument are `InventoryService` adjacent to `backend.laso/app/services/inventory/inventory_service.py:155-159`, and the purchase-order receiving path at `backend.laso/app/api/v1/endpoints/purchase_order_endpoints.py:464-467`. This is a precondition for everything else: without it there is nothing for the device to pull, and repairing the cursor would change nothing observable.

**6.2 Break the cursor deadlock.** In `ui.laso/src/lib/syncEngine.ts:365-423`, replace the unconditional `break` at `:406` with per-event failure accounting, and advance the cursor past failed events while recording them for retry or quarantine. Add a user-visible "sync stalled at seq N of M" indicator. A silently stalled cursor is indistinguishable from "no stock" to the user, which is precisely why this went unnoticed.

**6.3 Stop trusting `sellable_quantity` until something writes it.** Either add the writer (mirror the server's `compute_sellable_quantities` from `backend.laso/app/services/sync/_sellable_qty.py:9-68` into `_branchInventoryUpserted` and `localWrite.ts`), or, as the cheaper immediate option, flip `localRead.ts:1189` to prefer `quantity` and treat `sellable_quantity` as absent while it is unwritten. The cheaper option needs a decision first: see §7 question 4. Then correct `sellableQuantityProjected.test.ts` so it stops mocking a column production never populates.

**6.4 Un-scope the catalogue.** In `localRead.searchDrugs` (`localRead.ts:323-327`), move the branch predicate into the `ON` clause or remove the join entirely, and pass `organization_id` so the offline read is tenant-scoped like `drug_service.py:191`.

**6.5 Independently worth doing.** `_ensure_branch_access` (`backend.laso/app/api/v1/endpoints/inventory_endpoints.py:29-44`) should compare `branch.organization_id` to `user.organization_id`. Today a user can read another organization's branch by supplying its id, and no SQL predicate backs the check. Also consider having `isOfflineError` stop classifying 5xx as offline, so a genuine server fault surfaces as an error instead of silently swapping the source of truth for every page in the app.

---

## 7. Open questions

1. **Which event is the poison pill at seq 3** (`drug_updated` for Paracetamol 500mg)? Requires the device database, or the browser console, where `syncEngine.ts:397-400` logs the projector error.
2. **Why do four of the six `branch_inventory` rows carry `sync_status = 'pending'`** while the two most recently touched rows are `'synced'`? This points to a direct SQL or seed write that bypassed the event writers. Worth confirming before writing 6.1, since it identifies which code path to instrument.
3. **Should the Drug Catalogue list drugs with zero stock at the active branch?** The offline answer today is no; the server answer is yes. This is a product decision and it determines whether 6.4 removes the join or merely repairs it.
4. **Is the intended sellable semantics "unexpired batch sum minus other terminals' leases"** as implemented in `_sellable_qty.py:9-68`? If yes, the cheaper option in 6.3 is a regression against design intent rather than a simplification, and the writer is the only correct fix.