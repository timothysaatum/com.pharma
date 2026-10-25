# Offline Price Contracts - Phase 1 Plan

Date: 2026-10-25
Status: approved and implemented. See `price-contract-offline-pos-phase2-2026-10-25.md` for the shipped result.
Basis: `docs/investigations/price-contract-offline-pos-phase0-2026-10-25.md`
Approved direction: fix contract propagation, never let `/sync/events` invent a default contract.

## 1. Outcome

A cashier must always be able to complete a sale.

- Online: the server's contract list is authoritative. If it returns zero contracts, the sale is blocked with a clear reason.
- Offline with a cached usable contract: the cashier selects or auto-selects it, and the exact snapshot travels with `sale_created`.
- Offline with no cached usable contract: the sale completes at full price with `price_contract_id = NULL` and `contract_resolution = 'missing_offline'`. No default contract is fabricated locally or on the server.
- Verification, approval, preauthorization, and insurance contracts are never offered offline.
- The server validates the supplied contract's identity and org ownership. It validates branch, date, and online-only policy at `authored_at`, but it never reprices a completed sale. Policy failures are recorded as `invalid_client_snapshot`, not used to reject an already-completed sale.

## 2. Why the local cache needs an availability table

`price_contracts` alone cannot answer "is this contract available at branch X for user Y today?" The server answer depends on role, branch, date, status, and org. Caching only the row and re-deriving those filters locally is wrong in two ways:

- It leaks a contract available in branch B into branch A when applicability is role-based rather than branch-based.
- It cannot represent "the server returned zero contracts for this branch" without guessing.

The cache therefore stores two things:

1. `price_contracts`: metadata snapshot for display, discount, and historical matching.
2. `price_contract_availability`: the exact `(user, branch, contract)` set returned by `/contracts/available/{branch}`.

The availability table is the source of truth for offline selection. Local branch/date/status filters remain as defense in depth, but they no longer invent availability.

## 3. Design decisions

### D1 - Hydration writes a per-user, per-branch availability set

New local table:

```sql
CREATE TABLE price_contract_availability (
  organization_id TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  contract_id     TEXT NOT NULL,
  cached_at       TEXT NOT NULL,
  PRIMARY KEY (user_id, branch_id, contract_id)
);
CREATE INDEX idx_contract_availability_org_branch
  ON price_contract_availability (organization_id, branch_id);
```

Why user-scoped: `/contracts/available` is filtered by the signed-in user's roles. A shared pharmacy device must not show user A's role-gated contracts to user B while offline. Switching users online hydrates the new user's set. Switching users offline yields no contracts, which is the safe outcome.

The table is local-only. It is not added to the CRR set and is never pushed.

### D2 - Hydration is atomic and all-or-nothing per active branch

New module: `ui.laso/src/lib/contractCache.ts`.

Inputs: `userId`, `organizationId`, `activeBranchId`, `force`.

Branch discovery:

1. `user.assigned_branches` when non-empty.
2. Otherwise `branchApi.list({ is_active: true, page_size: 500 })` across all pages.
3. Always include `activeBranchId`.

Fetch `/contracts/available/{branchId}` for each branch with concurrency 4.

Replacement rules:

- If the active branch fails for any reason, abort. Keep the previous cache. Log once.
- If another branch returns 401/403/404, treat that branch as no longer accessible and exclude it from the replacement.
- If another branch fails with a network or 5xx error, abort. Keep the previous cache. Do not silently drop a branch's contracts.
- On success, one `BEGIN IMMEDIATE` transaction:
  - upsert every returned contract into `price_contracts`;
  - delete all `price_contract_availability` rows for `(organization_id, user_id)`;
  - insert the new mapping rows;
  - commit.
- Never delete `price_contracts` rows during hydration. Event projectors still need them, and orphan cleanup is not required for correctness.

Triggers:

- `SyncEngine.start` after login and after a branch switch (force).
- `SyncEngine.onOnline` and `BACKEND_CONNECTIVITY_EVENT` reachable (force).
- Every sync cycle via `ensureFresh()` with a 5 minute in-memory TTL.
- After pulling any `price_contract_*` event (force, debounced 2 seconds).

The hydration state is in-memory. A restart forces a refresh. No new local meta key is needed.

### D3 - `/contracts/available` returns the fields the cache needs

Extend `PriceContractService._format_contract_for_pos` additively:

```text
organization_id
effective_from
effective_to
status
is_active
applies_to_all_branches
applicable_branch_ids
```

Keep all existing fields. No field is removed or renamed. This is an additive API response change, not a sync protocol change.

The endpoint permission changes from `manage_pricing` to `require_any_permission("process_sales", "manage_pricing")`. The POS already requires this endpoint; a cashier with `process_sales` but not `manage_pricing` currently gets a 403. The endpoint's own role, branch, date, status, and org filters remain unchanged.

### D4 - Local reads use the availability mapping

`localRead.getAvailableContractsForPos(branchId, organizationId, userId)` becomes:

```sql
SELECT pc.*
  FROM price_contracts pc
  JOIN price_contract_availability a
    ON a.contract_id = pc.id
   AND a.branch_id = $branch
   AND a.user_id = $user
 WHERE pc.organization_id = $org
   AND pc.is_deleted = 0
   AND pc.is_active = 1
   AND pc.status = 'active'
   AND pc.effective_from <= $today
   AND (pc.effective_to IS NULL OR pc.effective_to >= $today)
   AND pc.contract_type <> 'insurance'
   AND pc.insurance_provider_id IS NULL
   AND pc.requires_verification = 0
   AND pc.requires_approval = 0
   AND pc.requires_preauthorization = 0
   AND (pc.applies_to_all_branches = 1 OR pc.applicable_branch_ids LIKE $branchJson)
 ORDER BY pc.is_default_contract DESC, pc.contract_name ASC
```

The branch check remains as defense in depth. The role/branch/date answer still comes from the server-created mapping. `requires_*` and insurance are excluded because this function is the offline/fallback path; verification cannot succeed offline. Online POS uses the server response, so those contracts still appear online.

### D5 - POS online and offline behavior diverge deliberately

`POSPage.loadContracts` becomes:

- Offline or backend known unreachable: read local mapping. Empty local set sets `contractsIssue.kind = 'offline'` and `allowMissingContract = true`.
- Online 200 with contracts: use the server list. Replace this user's mapping for this branch atomically with the returned set, and upsert the rows. Do not leave stale contract ids behind.
- Online 200 with zero contracts: clear this user's mapping for this branch atomically, set `contractsIssue.kind = 'empty'`, no local fallback, `allowMissingContract = false`, sale blocked.
- Online non-offline failure (401/403/404/5xx): `contractsIssue.kind = 'error'`, no local fallback, `allowMissingContract = false`, sale blocked. A server denial must not be bypassed with a stale cache.

`useCart` gains a `allowMissingContract` flag in state and `SET_ALLOW_MISSING_CONTRACT`. `validateCart` skips the contract error only when that flag is true. `buildSaleCreate` allows `price_contract_id` to be null only when that flag is true.

`POSPage` sets the flag from `isOffline && !contractsLoading && contracts.length === 0`. If the cache has contracts but the cashier did not select one, checkout remains blocked.

Offline empty-cache copy becomes: "Offline, and no usable price contract is stored on this device for this branch. This sale will be recorded without a contract."

Checkout path rule: if the built payload has no `price_contract_id`, force the offline sale path even if connectivity returned mid-checkout. This prevents the online `POST /sales` default-contract fallback from silently attaching a contract to a sale the cashier completed without one. If a contract is selected, normal online/offline path selection applies.

### D6 - Offline sale snapshot and marker

Local `sales` gains `contract_resolution TEXT`.

Offline sale values:

```text
contract_resolution = 'client'          when a contract was selected
contract_resolution = 'missing_offline' when no contract was selected
```

`SALE_COLUMNS` and `buildSaleCreatedEnvelope` add:

```text
price_contract_id
contract_name
contract_type
contract_discount_percentage
contract_resolution
```

`contract_name`, `contract_type`, and `contract_discount_percentage` come from the selected contract, or are null/0 when missing. Totals are not changed.

`recordOfflineSale` response uses the actual contract name, or "Unresolved (offline)", instead of hardcoded "Standard".

### D7 - Contract lifecycle events are emitted by endpoints

Add a full-snapshot payload builder in `PriceContractService`:

```text
id, organization_id, contract_code, contract_name, contract_type,
is_default_contract, discount_type, discount_percentage,
applies_to_prescription_only, applies_to_otc, applies_to_all_branches,
applicable_branch_ids, effective_from, effective_to,
requires_verification, requires_approval, daily_usage_limit,
per_customer_usage_limit, insurance_provider_id, requires_preauthorization,
minimum_purchase_amount, maximum_purchase_amount, status, is_active,
copay_amount, copay_percentage, is_deleted
```

Endpoint emissions, after the service commit:

| Endpoint | Event |
| --- | --- |
| `POST /contracts` | `price_contract_created` |
| `PATCH /contracts/{id}` | `price_contract_updated` |
| `DELETE /contracts/{id}` | `price_contract_deleted` |
| `POST /contracts/{id}/approve` | `price_contract_updated` |
| `POST /contracts/{id}/suspend` | `price_contract_updated` |
| `POST /contracts/{id}/activate` | `price_contract_updated` |
| `POST /contracts/{id}/duplicate` | `price_contract_created` |
| onboarding default seed | `price_contract_created` |

Use `ServerEventEmitter.emit` with `AggregateType.PRICE_CONTRACT`, the contract's org, `authored_by=current_user.id`, and the org-level nil branch. The service commits before the endpoint emits; the endpoint must `await db.commit()` after the emit so the event row is durable. Emitter failure is logged and swallowed by the existing emitter, so the primary write is never rolled back. Hydration heals a missed event.

For delete, fetch the contract snapshot before the soft delete and emit it after commit.

Client projectors:

- `_priceContractCreated` becomes an upsert instead of `INSERT OR IGNORE`. On conflict, update every metadata field, reset `is_deleted = 0`, preserve `created_at`.
- `_priceContractUpdated` remains a full-field update, with fallback to the upsert if the row is absent.
- `_priceContractDeleted` also deletes `price_contract_availability` rows for that contract.
- `applyEventLocally` emits `appEvents.emit("price-contracts:changed")` after any contract event. `POSPage` subscribes and reloads; `contractCache` subscribes and forces hydration.

### D8 - Server sale validation classifies, never reprices

`SaleProjector._validate_created` becomes async so it can query `price_contracts`.

Validation matrix:

| Payload | Result |
| --- | --- |
| No contract id, no marker or `missing_offline` | Accept. Persist `contract_resolution='missing_offline'`. |
| Contract id present, contract does not exist in the event's org | Reject permanently with `contract_not_found` / `contract_org_mismatch`. This is invalid identity, not policy. |
| Contract id present, soft-deleted | Accept. Historical sales must survive. |
| Contract id present, branch mismatch at `authored_at` | Accept snapshot. Persist `invalid_client_snapshot`. No reprice. |
| Contract id present, effective window does not cover `authored_at` | Accept snapshot. Persist `invalid_client_snapshot`. No reprice. |
| Contract id present, `requires_verification`, `requires_approval`, or `requires_preauthorization` | Accept snapshot. Persist `invalid_client_snapshot`. No reprice. |
| Contract id present, snapshot fields missing | Fill missing `contract_name`, `contract_type`, `contract_discount_percentage` from the DB row. Do not touch totals. |
| Marker and id contradict each other | Reject permanently with `contract_resolution_mismatch`. |

`_apply_created` adds `contract_resolution` to the `INSERT INTO sales`. It still does not call `compute_item_pricing` and does not apply a default.

Online `POST /sales` sets `contract_resolution`:

- `'client'` when `SaleCreate.price_contract_id` was supplied.
- `'server_default'` when the service resolved the org default because the field was null.

`SaleResponse` exposes the column. Existing rows remain null and are treated as legacy in reports.

## 4. Schema changes (approval required)

### Server migration

New revision `b7c8d9e0f1a2_add_sale_contract_resolution.py`, down revision `e7a8b9c0d1e4`.

```sql
ALTER TABLE sales ADD COLUMN contract_resolution VARCHAR(32) NULL;
ALTER TABLE sales ADD CONSTRAINT ck_sales_contract_resolution
  CHECK (
    contract_resolution IS NULL
    OR contract_resolution IN (
      'client',
      'server_default',
      'missing_offline',
      'invalid_client_snapshot'
    )
  );
```

Add the column to `app/models/sales/sales_model.py` and `SaleResponse` in `app/schemas/sales_schemas.py`.

No backfill. Old rows are null.

### Local migration

Bump `MAX_KNOWN_SCHEMA_VERSION` from 36 to 38. Add the missing `if (user_version < 38)` call. This also makes the existing unreachable `migrate_v37` run correctly on devices below 37.

New `migrate_v38`:

```sql
CREATE TABLE IF NOT EXISTS price_contract_availability (
  organization_id TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  contract_id     TEXT NOT NULL,
  cached_at       TEXT NOT NULL,
  PRIMARY KEY (user_id, branch_id, contract_id)
);
CREATE INDEX IF NOT EXISTS idx_contract_availability_org_branch
  ON price_contract_availability (organization_id, branch_id);
ALTER TABLE sales ADD COLUMN contract_resolution TEXT;
PRAGMA user_version = 38;
```

`ALTER TABLE` is guarded by a `PRAGMA table_info(sales)` check so a partially migrated DB cannot fail twice.

This migration is required because the availability set cannot be represented faithfully without it. There is no schema-free alternative that preserves role, branch, and empty-set semantics.

## 5. API changes (approval required)

Additive only:

- `GET /contracts/available/{branch_id}` adds the seven fields listed in D3.
- Permission changes to `require_any_permission("process_sales", "manage_pricing")`.

No sync protocol version bump. `price_contract_*` is already an `AggregateType`; the event types are already handled by the local projector. `sale_created` gains additive payload fields. Existing clients ignore them.

## 6. File-level change list

### Backend

- `backend.laso/migrations/versions/b7c8d9e0f1a2_add_sale_contract_resolution.py` (new)
- `backend.laso/app/models/sales/sales_model.py`: add `contract_resolution`.
- `backend.laso/app/schemas/sales_schemas.py`: expose `contract_resolution` on `SaleResponse`.
- `backend.laso/app/services/sales/sales_service.py`: set `client`/`server_default` on online sale.
- `backend.laso/app/services/sync/eventlog/projectors/sale.py`: async contract validation, classification, insert marker, snapshot fill.
- `backend.laso/app/services/contracts/price_contract_service.py`: add event payload builder; add the seven fields to `_format_contract_for_pos`.
- `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py`: emit lifecycle events; relax available permission.
- `backend.laso/app/services/org/organization_onboarding_service.py`: emit default-contract created event after commit.
- `backend.laso/scripts/backfill_stock_events.py`: already covers missing `price_contract_created` events. No new script. Reuse with dry run.

### UI

- `ui.laso/src/lib/localDb.ts`: v38 migration, MAX 38, availability table.
- `ui.laso/src/lib/priceContractStore.ts` (new): shared row upsert and atomic availability replacement.
- `ui.laso/src/lib/contractCache.ts` (new): branch discovery, fetch, TTL, debounce, hydration.
- `ui.laso/src/lib/localProjectors.ts`: upsert create, full update, delete availability, emit change event, no sale early return, contract fields update.
- `ui.laso/src/lib/localRead.ts`: join availability; user id parameter; offline filters; `toSaleWithDetails` uses real `contract_type`; map `contract_resolution`.
- `ui.laso/src/lib/localWrite.ts`: add contract fields and marker to envelope and `SALE_COLUMNS`.
- `ui.laso/src/lib/offlineSalesManager.ts`: marker flows through `buildLocalSalePayload`.
- `ui.laso/src/lib/syncEngine.ts`: accept `userId`; trigger hydration on start, reconnect, periodic, and contract events.
- `ui.laso/src/stores/authStore.ts`: pass `user.id` to `syncEngine.start`; optionally clear availability rows for the user on logout.
- `ui.laso/src/hooks/useCart.ts`: `allowMissingContract` state, validation, nullable payload.
- `ui.laso/src/pages/POSPage.tsx`: server-empty no fallback, offline allow, set flag, snapshot fields, real contract label.
- `ui.laso/src/api/contracts.ts`: extend `AvailableContract` with the seven fields.
- `ui.laso/src/types/index.ts`: add `contract_type` and `contract_resolution` to `Sale`.

## 7. Tests

### Backend

- Projector: no contract -> accepted, marker `missing_offline`, no default, no reprice.
- Projector: valid contract -> snapshot stored, marker `client`.
- Projector: missing snapshot fields -> filled from DB.
- Projector: soft-deleted contract -> accepted.
- Projector: wrong org / nonexistent -> permanent reject.
- Projector: branch/date/verification mismatch -> accepted, marker `invalid_client_snapshot`.
- Projector: marker/id contradiction -> permanent reject.
- Online sale: `client` and `server_default` values.
- Endpoint: each lifecycle action emits exactly one event with full payload.
- Endpoint: available response includes new fields and is reachable with `process_sales`.
- Onboarding: default contract created event emitted after commit.
- Migration: upgrade from head, downgrade, and existing rows remain null.

### UI

- Migration v38 creates the table and column, bumps version, and is idempotent on a partially migrated DB.
- Hydration: multiple branches, stale mapping removed, user scoping, active-branch failure keeps old cache, 403 branch excluded, concurrent calls coalesced.
- Local read: mapping required; branch/date/status/org filters; verification/approval/preauth/insurance excluded; no cross-user leakage.
- POS: online empty does not fall back; online error does not fall back; offline empty sets allow; offline populated requires selection.
- Cart: allows nullable contract only when flag is set; rejects null otherwise.
- Envelope: all contract fields and marker present.
- Local sale projector: existing local row gets contract fields and marker, preserves pending status.
- Contract projector: created twice updates; updated-before-created inserts; deleted removes availability.
- End-to-end: offline empty-cache sale -> outbox accepted -> server sale has null contract and `missing_offline`, no default.
- End-to-end: offline cached contract sale -> server sale has exact snapshot and `client`.
- End-to-end: reconnect after suspended contract -> hydration removes it from the offline set.

### Commands

```bash
cd backend.laso && source /home/vermithor/lasoenv/bin/activate && pytest
cd ui.laso && npm test
cd ui.laso && npx tsc --noEmit
```

## 8. Backfill, rollout, rollback

Backfill is not required for correctness of new sales. It is required only so devices that missed old contract events receive them.

1. Deploy backend migration and API changes. Old clients continue working.
2. Run `python scripts/backfill_stock_events.py` dry run. Verify the price-contract plan matches the org's contracts.
3. Back up `event_log` and `price_contracts`.
4. Run with `--apply --i-have-a-backup`.
5. Deploy the UI.
6. On first online launch, hydration populates availability.
7. Verify with the Phase 0 online/offline/recovery procedure.

Rollback:

- Server: the column and API fields are additive. Downgrade the migration or leave the column in place.
- UI: rolling back to a build that does not know schema v38 will hit the downgrade guard and refuse to start. Rollback requires restoring the pre-upgrade local DB backup or shipping a build with v38 support. This risk is called out for approval.
- Event log: the backfill only appends. Restoring it requires the backup.

## 9. Risks and failure modes

- **App rollback lockout.** Local v38 is newer than old builds. Mitigation: keep the upgrade window short; backup before deploy; rollback with DB restore.
- **Hydration request volume.** One request per branch. Mitigation: concurrency 4, 5 minute TTL, single in-flight promise. Revisit a bulk endpoint only if orgs exceed 20 branches.
- **Hydration failure leaves stale cache.** Mitigation: all-or-nothing on active branch and network/5xx. Stale cache is under-offer at worst, except role changes. Online POS never uses the cache.
- **Event emitter failure.** Contract metadata may lag until next hydration. Availability still refreshes on the 5 minute cycle.
- **Local clock wrong.** Local date filters may under- or over-offer. Availability is server-filtered at hydration; the local date check is defense in depth. No new clock dependency is introduced.
- **Server validation rejects a completed sale.** Only invalid identity (wrong org or nonexistent contract) rejects. Policy issues accept with `invalid_client_snapshot`, so a clock or branch edge case cannot strand a completed sale.
- **Shared device.** Availability is user-scoped; logout clears that user's rows. Price contract metadata rows are not user-scoped but contain no sales data.

## 10. Approval checklist

Approve these before Phase 2 starts:

- [ ] New local table `price_contract_availability`, user-scoped.
- [ ] Local schema version 38 and MAX_KNOWN_SCHEMA_VERSION 38.
- [ ] Server `sales.contract_resolution` column and check constraint.
- [ ] Additive `/contracts/available` response fields.
- [ ] Available endpoint permission change to `process_sales` or `manage_pricing`.
- [ ] Server-empty/online-error no longer falls back to local cache.
- [ ] Offline empty-cache sale is allowed with `missing_offline`.
- [ ] Policy-invalid supplied contracts are accepted with `invalid_client_snapshot`, not rejected.
- [ ] Contract lifecycle events emitted from endpoints and onboarding.
- [ ] Reuse `scripts/backfill_stock_events.py`; no new backfill script.

## 11. Out of scope

- Treating 5xx as offline for checkout.
- Offline sale/outbox atomicity changes.
- Accepted-outbox terminal failure recovery.
- The existing `MAX_KNOWN_SCHEMA_VERSION` mismatch beyond the v37/v38 fix above.
- Repricing completed sales.
- A server-side price-contract projector for client-pushed contract events. Contracts are server-authored.
- Bulk available-contracts endpoint.
