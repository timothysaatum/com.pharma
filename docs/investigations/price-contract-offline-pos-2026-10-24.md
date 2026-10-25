# Price contracts never reach the device: offline POS contract picker is empty

**Date:** 2026-10-24
**Branch / tip:** `fix/customers-sync` @ `2623202`
**Scope:** Why the POS shows an empty price-contract picker, the message "Offline, and no price contract is stored on this device for this branch", the validation message "Select a price contract", and a disabled Complete Sale button, while the server has an active default contract for the same organization and branch.
**Method:** Static trace of the contract, POS, sync, and sale paths in `ui.laso` and `backend.laso`; read-only SQL against PostgreSQL `atlasdb`; read-only inspection of the device's SQLCipher database `laso.db` and of the persisted session in `laso.bin`. No code, migration, or data was changed. No fix was applied.
**Status:** Root cause identified. The contract exists in Postgres, but no `price_contract_created` event was ever written, so the event-sourced device never learns about it.

Claims are marked **VERIFIED** (code read, query run, or output observed) or **INFERRED** (reasoned from verified evidence).

---

## 1. Verdict

**The server has the contract. The device has no way to know it exists.**

The organization's default contract was created by onboarding through a direct database write. That path appends no `price_contract_created` event. The desktop sync loop is event-only, so it pulls only the seven events that do exist (drug category, drug, branch inventory, batch, inventory update, and two sales). The device's local `price_contracts` table stays empty. When the POS is offline, its contract source is that local table, so `getAvailableContractsForPos` returns `[]`. The empty list leaves the picker with no options, `CartPanel`'s auto-select effect never runs, `validateCart` pushes "Select a price contract", and the Complete Sale button is disabled.

This is not a branch, date, role, organization, or projection bug. The contract's own flags all pass the POS filters. The only missing input is the event.

Causal chain:

```
onboarding creates PriceContract row
        |
        +-- no price_contract_created event
                |
                +-- device event pull applies 7 events, none for contracts
                        |
                        +-- local price_contracts = 0 rows
                                |
                                +-- offline loadContracts() = []
                                        |
                                        +-- picker empty, no auto-select
                                                |
                                                +-- "Select a price contract"
                                                        |
                                                        +-- sale button disabled
```

---

## 2. Evidence at a glance

| Fact | Value | Status |
|---|---|---|
| Server contract row | `9ce66829-e694-4d2e-b16d-dedabecd214c` (`STANDARD`, `Standard Pricing`, `standard`) | **VERIFIED** |
| Contract flags | `status=active`, `is_active=t`, `is_default_contract=t`, `is_deleted=f`, `applies_to_all_branches=t`, `effective_from=2026-10-24`, `effective_to=NULL`, `allowed_user_roles=[]`, discount `0.00` | **VERIFIED** |
| Contract events in server log | 0 | **VERIFIED** |
| Server event log for this org | 7 rows, seq 1 through 7, none for a price contract | **VERIFIED** |
| Device `price_contracts` rows | 0 | **VERIFIED** |
| Device event cursor for this org | `event_pull_seq:cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8 = 7` | **VERIFIED** |
| Device CRR flags | `crr_enabled_* = 0` for every table, including `price_contracts` | **VERIFIED** |
| Device sales | 2 completed sales today, ₵5.00 each, total ₵10.00 | **VERIFIED** |
| Server sales | 2 completed sales today, both with the contract snapshot populated | **VERIFIED** |
| Online POS contract source | `contractsApi.getAvailableForPos` against the live API | **VERIFIED** (two online sales succeeded) |
| Offline POS contract source | local `price_contracts` only | **VERIFIED** |
| Backfill script | Exists, emits the missing event, not applied to this org | **VERIFIED** (script and commit) |

Real identifiers used throughout:

| Entity | ID | Name |
|---|---|---|
| Organization | `cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8` | GreenLight Pharmacy |
| Branch | `521ef6b1-437f-4cd5-878d-59d51902871e` | Berekum HFH (BR001) |
| Contract | `9ce66829-e694-4d2e-b16d-dedabecd214c` | Standard Pricing |
| Admin / cashier | `1d2a0f2a-e174-4f30-b1ea-6bc0799e33af` | GreenLight admin |

---

## 3. A. Contract model and creation

### 3.1 The server row is valid

The contract is the organization's default, applies to every branch, is active, and is inside its effective window. It also permits every user role because `allowed_user_roles` is empty.

**VERIFIED** by `SELECT` against `price_contracts`.

### 3.2 All POS eligibility filters pass

`PriceContractService.get_available_contracts_for_pos` (`backend.laso/app/services/contracts/price_contract_service.py:812-848`) requires:

- `organization_id == user.organization_id`
- `is_deleted == False`
- `is_active == True`
- `status == "active"`
- `effective_from <= today`
- `effective_to IS NULL OR effective_to >= today`
- `applies_to_all_branches == True`, or the branch ID is in `applicable_branch_ids`
- `allowed_user_roles` is empty, or the user's role is listed

Every condition passes for this row. The branch filter is not the cause.

### 3.3 The row is created without an event

**VERIFIED** in `backend.laso/app/services/org/organization_onboarding_service.py`:

- The call site at `:180` creates the default contract during onboarding.
- `_create_default_price_contract` at `:359-380` builds a `PriceContract`, calls `self.db.add(contract)`, and calls `await self.db.flush()`.
- There is no `StockEventEmitter.emit_in_transaction`, no `append_event`, and no `event_log` write in that method or anywhere in the onboarding service.

The contract CRUD path has the same gap:

- `create_contract` at `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py:46-84` calls the service and returns the model.
- `PriceContractService.create_contract` at `price_contract_service.py:75` adds and commits the row.
- Update and delete paths do not emit events either.
- A repo-wide search for `price_contract_created` finds it only in `backend.laso/scripts/backfill_stock_events.py` and its integration test.

### 3.4 The device projector exists and is wired

**VERIFIED** in `ui.laso/src/lib/localProjectors.ts`:

- The dispatch table handles `price_contract_created`, `price_contract_updated`, and `price_contract_deleted` at `:129-136`.
- `_priceContractCreated` at `:717-780` inserts the row into local `price_contracts`.
- `_priceContractUpdated` and `_priceContractDeleted` update or soft-delete the row.

The projector is correct enough for the current payload. It never runs because no event arrives.

### 3.5 The backfill script already describes this failure

`backend.laso/scripts/backfill_stock_events.py:35-41` says:

> "the real org's STANDARD-PRICE contract had no price_contract_created event, so no device could offer it, the POS contract picker stayed empty and the sale button never enabled."

The script emits `price_contract_created` at `:439-500`. The payload mirrors the server row field for field. The commit that added it (`538ebe0`, 2026-10-03) explicitly states it was **not applied**.

---

## 4. B. Organization, branch, and local lookup

### 4.1 The device is in the right tenant and branch

**VERIFIED** from `laso.bin`:

- `cache.organization.id = cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8` (GreenLight Pharmacy)
- `cache.branches[0].id = 521ef6b1-437f-4cd5-878d-59d51902871e` (Berekum HFH)
- The auth user's `assigned_branches = [521ef6b1-437f-4cd5-878d-59d51902871e]`
- The session branch is the same branch.

The server has one branch and one contract for this organization. The branch and organization match on both sides.

### 4.2 The local query is org-scoped and branch-scoped

**VERIFIED** in `ui.laso/src/lib/localRead.ts:887-925`:

```sql
SELECT * FROM price_contracts
 WHERE is_deleted = 0
   AND is_active = 1
   AND status = 'active'
   AND effective_from <= $1
   AND (effective_to IS NULL OR effective_to >= $1)
   AND (applies_to_all_branches = 1 OR applicable_branch_ids LIKE $2)
   AND organization_id = $3
 ORDER BY is_default_contract DESC, contract_name ASC
```

The organization filter is mandatory. If `organizationId` is missing the method returns `[]` rather than every contract on the device. That is deliberate, from commit `11dfbfc`.

POS passes the signed-in user's organization at `ui.laso/src/pages/POSPage.tsx:188-191`. There is no organization-filter defect in this incident.

### 4.3 The branch and date filters are irrelevant because the table is empty

`SELECT count(*) FROM price_contracts` on the device returns 0. The row is not being filtered out. It was never inserted.

---

## 5. C. Pricing and why checkout is blocked

### 5.1 Pricing calculation

`computeTotals` at `ui.laso/src/hooks/useCart.ts:283-327`:

- `subtotal = sum(unit_price * quantity)`
- `discountAmount = subtotal * contract.discount_percentage / 100`
- `taxAmount` is estimated from the average item tax rate, with tax-inclusive handling
- `total` is `taxableAmount` when tax-inclusive, otherwise `taxableAmount + taxAmount`
- For insurance contracts, `patientCopay` and `amountDue` are derived from copay amount or copay percentage
- `change = max(0, amountPaid - amountDue)`

With the `Standard Pricing` contract the discount is 0%, so the total is the subtotal plus any tax. The pricing is not the blocker.

### 5.2 The UI requires a contract

**VERIFIED** in `useCart.ts:368-370`:

```ts
if (!state.contract) {
    errors.push({ field: "contract", message: "Select a price contract" });
}
```

`buildSaleCreate` at `:513-515` throws `"No contract selected"` and sends `state.contract.id` as `price_contract_id`.

`CartPanel` disables the button when any validation error exists (`components/pos/CartPanel.tsx:1216`):

```tsx
disabled={isSubmitting || validationErrors.length > 0}
```

The same errors are rendered above the button at `:1188-1196`. This is why the visible message and the disabled CTA appear together.

### 5.3 The backend is looser than the UI

**VERIFIED**:

- `price_contract_id` is optional in `backend.laso/app/schemas/sales_schemas.py:240-243`.
- `load_and_validate_contract` at `backend.laso/app/services/sales/validators/sale_validators.py:142-225` falls back to the organization's `is_default_contract` when the ID is null (see `:198-205`).

A sale without an explicit contract would succeed on the current backend. The POS never reaches that path because its own validation blocks checkout first.

### 5.4 Why the picker stays empty

- `CartPanel`'s auto-select effect returns immediately when `contracts.length === 0` (`CartPanel.tsx:525`).
- `pickDefaultContract` returns null for an empty or missing list (`ui.laso/src/lib/pickDefaultContract.ts:31-40`).
- The placeholder option is rendered only when the list is empty (`CartPanel.tsx:925-929`), and selecting it clears the contract, which reproduces the error.

There is no fallback that selects the server's default contract by ID when the list is empty. Even if there were, the offline path has no server call to make.

---

## 6. D. Sync and offline behavior

### 6.1 The desktop sync loop is event-only

**VERIFIED** in `ui.laso/src/lib/syncEngine.ts`:

- `pullEvents` at `:442-458` calls `syncApi.pullEvents(afterSeq)`.
- Each returned envelope is applied through `applyEventLocally`.
- The loop is cursor-based per organization.
- The only other `pullEvents` calls are failure retry (`:683`) and aggregate replay (`:800`).

There is no call to the legacy `/sync/pull` endpoint from the UI. The only mention is a comment in `ui.laso/src/lib/localDb.ts:1206`.

### 6.2 The server has a full-pull path that includes contracts, but it is unused

**VERIFIED** in `backend.laso/app/services/sync/sync_service.py`:

- `SYNC_TABLES` includes `price_contracts` at `:115-126`.
- The full-pull branch returns `PriceContractResponse` rows at `:626-630`.

This path would have delivered the contract. The desktop app does not use it, so it does not help.

### 6.3 The pulled log contains no contract event

The organization's `event_log` has exactly seven rows:

| seq | event_type |
|---|---|
| 1 | `drug_category_created` |
| 2 | `drug_created` |
| 3 | `branch_inventory_created` |
| 4 | `drug_batch_created` |
| 5 | `branch_inventory_updated` |
| 6 | `sale_created` |
| 7 | `sale_created` |

There is no `price_contract_created`, `price_contract_updated`, or `price_contract_deleted` row anywhere in the database. **VERIFIED** by a count over the whole table.

The device's cursor is 7, so it has consumed the entire log. There is nothing else to pull.

### 6.4 CRR is disabled

**VERIFIED** in the device `sync_meta`:

- `crr_enabled_price_contracts = 0`
- every other `crr_enabled_*` flag is 0.

There is no alternate replication path for contracts.

### 6.5 The online result is not cached locally

**VERIFIED** by source search:

- No source file writes `AvailableContract` rows into local `price_contracts` outside the projector.
- The only local `price_contracts` insert is `_priceContractCreated` at `localProjectors.ts:721`.
- `POSPage` uses the online list only in component state (`setContracts(data)`), not in the local database.

Consequence: a POS session that was online and successfully selected a contract still has an empty local contract table when the connection drops.

### 6.6 Offline load and checkout behavior

**VERIFIED** in `ui.laso/src/pages/POSPage.tsx:181-243`:

```ts
if (isOffline || !navigator.onLine || isBackendKnownUnreachable()) {
    const local = await loadLocalContracts();
    setContracts(local);
    setContractsIssue(
        local.length > 0
            ? { kind: null, message: null }
            : {
                  kind: 'offline',
                  message:
                      'Offline, and no price contract is stored on this device for this branch.',
              }
    );
    return;
}
```

`loadLocalContracts` is `localRead.getAvailableContractsForPos(activeBranchId, user?.organization_id)`.

Offline detection is defined in `ui.laso/src/api/client.ts:32-50` and uses `navigator.onLine`, `isBackendReachable()`, and `isBackendKnownUnreachable()`. The sync engine sets `offline` status when the browser or backend is unreachable (`syncEngine.ts:169-190`).

The empty local list is therefore rendered as the offline message, and the cart validation blocks the sale.

### 6.7 `applied_events = 0` is not evidence of a failure

**VERIFIED** in `localProjectors.ts`: only guarded projectors call `markEventApplied`. `_saleCreated` and `_priceContractCreated` are unguarded. A zero count is expected for this log and does not mean the seven events failed to apply.

---

## 7. E. Sale recording and contract snapshots

### 7.1 The server sale row has the contract snapshot

**VERIFIED** in `backend.laso/app/services/sales/sales_service.py:837-840`:

```python
price_contract_id=contract.id,
contract_name=contract.contract_name,
contract_discount_percentage=float(contract.discount_percentage),
contract_type=contract.contract_type,
```

Both existing sales reference `9ce66829-e694-4d2e-b16d-dedabecd214c`, `Standard Pricing`, `standard`, with discount `0.00`.

### 7.2 The `sale_created` payload omits the contract snapshot

**VERIFIED** in `sales_service.py:1318-1345`. The payload contains organization, branch, sale number, customer, cashier, pharmacist, payment fields, totals, prescription, items, and batch changes. It does not contain any of:

- `price_contract_id`
- `contract_name`
- `contract_discount_percentage`
- `contract_type`

The device projector expects the first three: `_saleCreated` inserts `price_contract_id`, `contract_name`, and `contract_discount_percentage` from the payload at `localProjectors.ts:419,443`. An event applied on another device therefore writes null contract fields.

### 7.3 The offline envelope has the same gap

**VERIFIED** in `ui.laso/src/lib/localWrite.ts:212-262`. `buildSaleCreatedEnvelope` payload keys do not include any contract field. The local sale itself may have them, but the event shipped to the server and to other devices does not.

Server sync `_apply_sale` accepts `price_contract_id` and clears it when the contract is missing or outside the organization (`sync_service.py:348-386`). An offline sale that loses its contract ID can therefore round-trip to the server with a null contract.

### 7.4 Local sale rows: unexplained combination

**VERIFIED** local rows:

- 2 sales, both `sync_status='synced'`
- `price_contract_id=9ce66829...`
- `contract_name='Standard Pricing'`
- `contract_type=NULL`
- `items_json='[]'`
- `items_count=1`

No current code path reproduces all of those together:

- The offline path writes contract fields and a populated `items_json`.
- The server `_saleCreated` projector writes `items_json` from the payload but no contract fields (the payload has none).
- A later update that clears `items_json` was not found.

The exact provenance is unresolved. It does not affect the contract root cause, but it is a separate data-shape concern.

---

## 8. F. Today's Sales

The header renders `fmtGHS(todaySales)` at `POSPage.tsx:697`.

### 8.1 Online

`statsApi.getBranchStats(activeBranchId)` returns `total_sales_today`. The backend computes it from `sum(Sale.total_amount)` for the day (`backend.laso/app/services/branch/branch_service.py:500-536`).

### 8.2 Offline

`POSPage.tsx:74-82` sums local completed sales for the current UTC date:

```ts
const today = new Date().toISOString().slice(0, 10);
const localSales = await localRead.searchSales({
    branch_id: activeBranchId,
    start_date: today,
    end_date: today,
    status: "completed",
    page_size: 1000
});
const total = localSales.items.reduce(
    (sum, s) => sum + Number(s.total_amount || 0),
    0
);
```

The device has two completed sales at ₵5.00 each, total ₵10.00. The local sum is 10.00. The server sum is also 10.00. Both paths explain the displayed value.

---

## 9. G. History

| Commit | Date | Relevance |
|---|---|---|
| `6153f23` | 2026-08-05 | Added the default contract seeding during offline-first hardening. The seed made server-side sales work but did not make the contract event-sourced. |
| `ce5f7453` | 2026-05-24 | Made the UI require a contract before checkout. |
| `32100bf` | 2026-10-03 | Added the `offline` / `empty` / `error` contract messages and the Retry action. |
| `86f00ac` | 2026-10-04 | Fixed the one-shot auto-select latch in `CartPanel`. |
| `11dfbfc` | 2026-10-03 | Made the organization filter mandatory and purged poisoned local contract rows. |
| `538ebe0` | 2026-10-03 | Added the `price_contract_created` backfill and documented this exact failure. The commit states the backfill was **not applied**. |
| `f48a555`, `54b0483` | 2026-10-03 | Related backfill work. |
| `898c05a` | 2026-07-15 | Offline CRR sync and POS fallback changes. |

The current branch is `fix/customers-sync` at `2623202`.

---

## 10. Fix plan

No fix was applied. The following is the complete plan for a later change.

### 10.1 Emit the event at creation

In `_create_default_price_contract` (`organization_onboarding_service.py:359-380`), after `db.add(contract)` and `flush()`, append `price_contract_created` through `StockEventEmitter.emit_in_transaction`, in the same transaction as the contract row.

The payload must include every field `_priceContractCreated` reads:

- `id`, `organization_id`
- `contract_code`, `contract_name`, `contract_type`
- `status`, `is_active`, `is_default_contract`
- `discount_type`, `discount_percentage`
- `applies_to_prescription_only`, `applies_to_otc`, `applies_to_all_branches`
- `applicable_branch_ids`
- `effective_from`, `effective_to`
- `requires_verification`, `requires_approval`, `requires_preauthorization`
- `copay_amount`, `copay_percentage`, `insurance_provider_id`
- `daily_usage_limit`, `per_customer_usage_limit`
- `minimum_purchase_amount`, `maximum_purchase_amount`

The backfill's payload builder at `backfill_stock_events.py:439-500` is the reference shape. It also sends `description`, which the local projector ignores. The local schema has no `description` or `allowed_user_roles` column; the local POS does not filter by role, the online endpoint does.

### 10.2 Emit events from contract CRUD

Add `price_contract_created`, `price_contract_updated`, and `price_contract_deleted` emission to the contract service or endpoints, inside the same transaction as the row change.

The device already has projectors for all three event types, so no new projector is needed for the current row shape.

### 10.3 Backfill the contracts that already exist

Run `backend.laso/scripts/backfill_stock_events.py` scoped to each organization:

```bash
python scripts/backfill_stock_events.py \
  --org-id <org-id> \
  --branch-id <branch-id> \
  --author-id <author-id>
```

Then, after a `pg_dump`:

```bash
python scripts/backfill_stock_events.py \
  --org-id <org-id> \
  --branch-id <branch-id> \
  --author-id <author-id> \
  --apply --i-have-a-backup
```

For the local GreenLight organization the IDs are:

- org `cfa1d31b-bf34-46f5-b5e1-2da4ad9519d8`
- branch `521ef6b1-437f-4cd5-878d-59d51902871e`
- author `1d2a0f2a-e174-4f30-b1ea-6bc0799e33af` (the contract's `created_by` and the current admin user)

The script's defaults are production IDs and must not be used for this local org. The backfill is dry-run by default and sets the PostgreSQL transaction to READ ONLY, so a dry run cannot write.

### 10.4 Make the projector repair stale rows

Change `_priceContractCreated` from `INSERT OR IGNORE` to an upsert that updates every non-key column on conflict. The current `INSERT OR IGNORE` cannot repair a poisoned or stale local row, which is the failure mode documented in `11dfbfc`. This is not the cause of the current empty table, but it is the same trap on a replayed event.

### 10.5 Carry the contract snapshot in sale events

Add the contract fields to both:

- `buildSaleCreatedEnvelope` in `ui.laso/src/lib/localWrite.ts`
- `_event_payload` in `backend.laso/app/services/sales/sales_service.py`

At minimum: `price_contract_id`, `contract_name`, `contract_discount_percentage`, `contract_type`. The device projector already has columns for the first three; `contract_type` exists in local `sales` through the `addSaleCol("contract_type TEXT")` migration at `localDb.ts:1108-1110`.

Without this, a sale authored on one device remains contract-less on every other device even after the contract event is fixed.

### 10.6 Optional write-through cache

The online POS list could be cached into local `price_contracts` after a successful load, so an offline session still works if the event path is ever delayed. This is a resilience layer, not the source-of-truth fix. If added, mark cached rows so a later event can overwrite them, and keep the existing organization filter mandatory.

### 10.7 Tests and evals

Add, in the same change:

1. Backend integration test: onboarding creates the default contract **and** exactly one `price_contract_created` event with the full payload, in the same transaction.
2. Backend integration test: contract create, update, and delete each append the expected event.
3. Device test: apply a `price_contract_created` envelope, then call `getAvailableContractsForPos` and assert the contract is returned for the org and branch.
4. Device test: offline POS with a synced contract completes a sale and sends `price_contract_id`.
5. Device test: apply a `sale_created` envelope with contract fields and assert the local sale row keeps them.
6. Eval: after one online contract sync, disconnect and verify the POS picker is populated and checkout is enabled on a fresh session.
7. Keep and extend the existing backfill coverage in `backend.laso/tests/integration/test_backfill_stock_events.py`.

The existing tests already cover the backfill's event shape and dry-run behavior. They do not cover the onboarding or CRUD emission, because those paths do not emit.

---

## 11. Things I could not verify

1. **Production state.** Only the local `atlasdb` was inspected. I do not know whether the production organization has a `price_contract_created` event or whether the backfill was applied there. Commit `538ebe0` says it was not applied as of 2026-10-03.
2. **Live online contract response.** The backend was stopped (`connection refused` on `127.0.0.1:8000`), so I did not call `GET /contracts/available/{branch_id}` live. The two successful server sales, the SQL row, and the filter logic prove the online path works.
3. **Exact provenance of the two local sale rows.** The combination of populated contract fields, `contract_type=NULL`, and `items_json='[]'` with `items_count=1` is not reproduced by any current code path. It is a separate data-shape question.
4. **The app's backend target at the exact time of the screenshot.** The current `laso.bin` tenant and the local `atlasdb` rows are consistent with the local backend. I cannot prove from the persisted file alone which URL the app used at that moment.
5. **Why `applied_events` is zero.** This is expected for unguarded projectors and is not evidence that the seven events failed. I did not instrument the pull to prove per-event application beyond the resulting local rows.

---

## Appendix A. Read-only DB access

PostgreSQL was queried with `psql` against `atlasdb` using SELECT statements only. The device database was copied to `/tmp` and opened read-only with a small Rust `rusqlite` tool linked against `bundled-sqlcipher`. The SQLCipher key was read from the GNOME keyring through `libsecret` and is deliberately not reproduced in this document.

The device database path is:

```
~/.local/share/com.vermithor.pharmacare/laso.db
```

The persisted session and cache path is:

```
~/.local/share/com.vermithor.pharmacare/laso.bin
```

Both were read after the app's two sales had completed. No write was issued to either database or to PostgreSQL.

## Appendix B. Key file map

| Concern | File |
|---|---|
| POS contract load and offline messages | `ui.laso/src/pages/POSPage.tsx` |
| Cart contract validation and totals | `ui.laso/src/hooks/useCart.ts` |
| Picker, auto-select, disabled CTA | `ui.laso/src/components/pos/CartPanel.tsx` |
| Online contract API | `ui.laso/src/api/contracts.ts` |
| Local contract read | `ui.laso/src/lib/localRead.ts` |
| Local contract schema and migrations | `ui.laso/src/lib/localDb.ts` |
| Contract projectors | `ui.laso/src/lib/localProjectors.ts` |
| Offline sale envelope | `ui.laso/src/lib/localWrite.ts` |
| Event pull loop | `ui.laso/src/lib/syncEngine.ts` |
| Onboarding default contract | `backend.laso/app/services/org/organization_onboarding_service.py` |
| Contract endpoints | `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py` |
| Contract service and POS filter | `backend.laso/app/services/contracts/price_contract_service.py` |
| Sale contract snapshot and event payload | `backend.laso/app/services/sales/sales_service.py` |
| Optional-contract fallback | `backend.laso/app/services/sales/validators/sale_validators.py` |
| Legacy full-pull contracts | `backend.laso/app/services/sync/sync_service.py` |
| Event backfill | `backend.laso/scripts/backfill_stock_events.py` |
