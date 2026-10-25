# Offline Price Contracts - Phase 2 Report

Date: 2026-10-25
Status: implemented and verified with automated tests.
Basis: `price-contract-offline-pos-2026-10-24.md`, `price-contract-offline-pos-phase0-2026-10-25.md`, `price-contract-offline-pos-phase1-2026-10-25.md`.

## 1. Outcome

An offline POS can complete a sale in both states the cashier can actually reach:

- A usable contract is cached for this user, branch, and day: the contract is offered and its exact snapshot travels with the sale.
- No usable contract is cached: the cashier sees "No contract (offline sale)", checkout is enabled, and the sale is recorded with `price_contract_id = NULL` and `contract_resolution = 'missing_offline'`.

The server no longer rejects a deferred offline sale because a contract snapshot was filled in before the append hash check.

## 2. What changed

### Backend

- `sales.contract_resolution VARCHAR(32) NULL` plus `check_sale_contract_resolution`; existing rows stay NULL.
- `SaleProjector` contract validation is pure. It checks identity, org ownership, marker/id contradictions, and accepts policy failures for historical sales.
- Snapshot backfill and marker classification happen at apply time in `_resolved_sale_contract`. The event payload is never mutated.
- Policy failures at `authored_at` are recorded as `invalid_client_snapshot`; totals are never repriced.
- Online sales record `client` when the request supplied a contract and `server_default` when the service chose the default.
- `sale_created` event payloads carry contract id, name, type, discount, and resolution.
- Available-contracts response carries organization, discount type, date window, status, branch scope, and applicable branch ids.
- `/contracts/available/{branch_id}` accepts either `process_sales` or `manage_pricing`.
- Contract create, update, delete, approve, suspend, activate, and duplicate emit a full contract event after the primary write commits.
- Organization onboarding emits the default contract event after onboarding commits.

### UI

- Local schema v38 adds `price_contract_availability`, keyed by user, branch, and contract, plus `sales.contract_resolution`.
- `priceContractStore.ts` atomically replaces availability and upserts contract metadata. Availability is user and branch scoped, never cross-org.
- `contractCache.ts` hydrates from `user.assigned_branches` or a paged active-branch list, always includes the active branch, limits concurrency to 4, keeps a 5-minute TTL, and preserves the previous cache on active-branch or branch-list failure.
- Hydration runs on `SyncEngine.start`, reconnect, normal sync completion, and a debounced `price-contracts:changed` event.
- POS online 200 with contracts replaces that branch mapping. Online 200 empty clears the mapping and blocks checkout. Any online error blocks checkout and does not fall back to cache.
- POS offline reads only the local user, branch, org mapping. Offline empty enables the missing-contract path; offline populated stays contract-driven.
- A null contract forces the offline sale path even if connectivity returns mid-checkout, so the client and outbox agree on the snapshot.
- Local `sale_created` projection writes contract type and resolution, updates contract metadata on an existing row without moving stock again, and defaults `change_amount` to 0 so a server event cannot be silently ignored by `INSERT OR IGNORE`.
- Contract projectors fall back create-to-update and update-to-create instead of recursing, emit `price-contracts:changed`, and delete availability on a contract delete.

## 3. Decisions and corrections

- Contract events are immutable after append. Projector validation runs before the append path verifies `hash_self`; mutating the payload there produced `HASH_MISMATCH` and permanent rejection of valid deferred sales. Resolution now happens only during apply.
- The event log can never be uniformly normalized because deferred events are appended before validation. Re-resolving from the current contract row at apply time is the only consistent option.
- Missing contract is represented as `NULL` plus `missing_offline`, never as a fabricated default. No local or server value invents a contract.
- Policy failures do not reject a completed sale. Only invalid identity or marker/id contradictions reject.
- Local availability is not a CRR table. It is device-local user state and must not replicate across users or devices.
- Online empty is authoritative for that branch. Cache is a fallback only when the device is offline.

## 4. Verification

### UI

```bash
cd ui.laso
npx tsc --noEmit
npx vitest run
```

Final UI result: 83 files, 620 tests passed.

Focused regressions:

- `src/lib/__tests__/priceContractCache.spec.ts`: hydration, branch and user scope, TTL, force, concurrent coalescing, active failure preservation, branch discovery failure.
- `src/lib/__tests__/posPriceContracts.spec.ts`: local availability mapping, filters, cross-user isolation, lifecycle projectors, sale contract update without double stock, outbox envelope fields.
- `src/lib/__tests__/localDbMigrationV38.test.ts`: v38 creates the table and column and is re-runnable.
- `src/hooks/__tests__/useCart.test.tsx`: null contract rejected by default and accepted only in missing-offline mode.
- `src/pages/__tests__/POSPage.offline.test.tsx`: offline empty cache enables missing-contract mode and does not offer a cached contract.
- `src/lib/__tests__/liveBackfillReplay.test.ts`: replayed sale and contract events stay idempotent.

### Backend

```bash
cd backend.laso
source /home/vermithor/lasoenv/bin/activate
PYTHONPATH=. TEST_DATABASE_URL='postgresql+asyncpg://postgres@/com_pharma_phase2?host=/tmp/com-pharma-phase2-pg/sock&port=55998' pytest
```

Final backend result: 461 tests passed.

Focused regressions:

- `tests/integration/test_sale_sync_projector.py`: no contract, valid client contract, missing snapshot backfill, branch mismatch, expired contract, verification policy, soft delete, wrong org, marker/id contradiction.
- `tests/integration/test_sales_contracts.py`: explicit and default online sale markers.
- `tests/unit/test_price_contract_events.py`: one event per lifecycle helper call and swallowed emitter failure.
- `tests/integration/test_backfill_stock_events.py`: socket-style `DATABASE_URL` parsing now works for the backfill script.

The full backend run used a disposable PostgreSQL 16 cluster at `/tmp/com-pharma-phase2-pg` on port 55998. Production was not contacted.

## 5. Rollout

1. Deploy the backend and run `alembic upgrade head`.
2. Dry-run the backfill and inspect the price-contract plan:
   `python scripts/backfill_stock_events.py --dry-run --org-id <org> --branch-id <branch> --author-id <user>`
3. Back up `event_log` and `price_contracts`, then apply:
   `python scripts/backfill_stock_events.py --apply --i-have-a-backup --org-id <org> --branch-id <branch> --author-id <user>`
4. Deploy the UI. On first online launch, hydration populates availability.
5. Verify online, offline with a cached contract, offline empty cache, and reconnect.

### Restart after deploy

From the repo root, restart the backend with the project script. It stops only its own listener, runs `alembic upgrade head`, and starts uvicorn:

```bash
./start_backend.sh
```

Restart the desktop app so the new local schema v38 runs.

### Rollback

Once local v38 has run, older UI builds refuse to start against that database. Rollback requires restoring the pre-v38 local database or shipping a v38-aware build. Backend rollback is reversible with `alembic downgrade -1`.

## 6. Residual risks

- A contract's offer snapshot is trusted for name, type, and discount if supplied. The server validates identity and policy, not snapshot equality. Totals are intentionally not repriced.
- A hard-deleted contract between validation and apply raises a projector error and retries. Soft delete is the supported path.
- The packaged Tauri app was not launched against production after this change. The post-deploy checks in step 5 still need to run on a real device.

## 7. Files

Backend:

- `backend.laso/migrations/versions/b7c8d9e0f1a2_add_sale_contract_resolution.py`
- `backend.laso/app/models/sales/sales_model.py`
- `backend.laso/app/schemas/sales_schemas.py`
- `backend.laso/app/services/sync/eventlog/projectors/sale.py`
- `backend.laso/app/services/sales/sales_service.py`
- `backend.laso/app/services/contracts/price_contract_service.py`
- `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py`
- `backend.laso/app/services/org/organization_onboarding_service.py`
- `backend.laso/scripts/backfill_stock_events.py`
- `backend.laso/tests/integration/test_sale_sync_projector.py`
- `backend.laso/tests/integration/test_sales_contracts.py`
- `backend.laso/tests/unit/test_price_contract_events.py`

UI:

- `ui.laso/src/lib/localDb.ts`
- `ui.laso/src/lib/localRead.ts`
- `ui.laso/src/lib/localWrite.ts`
- `ui.laso/src/lib/localProjectors.ts`
- `ui.laso/src/lib/priceContractStore.ts`
- `ui.laso/src/lib/contractCache.ts`
- `ui.laso/src/lib/syncEngine.ts`
- `ui.laso/src/lib/events.ts`
- `ui.laso/src/stores/authStore.ts`
- `ui.laso/src/api/contracts.ts`
- `ui.laso/src/types/index.ts`
- `ui.laso/src/hooks/useCart.ts`
- `ui.laso/src/pages/POSPage.tsx`
- `ui.laso/src/components/pos/CartPanel.tsx`
- `ui.laso/src/lib/__tests__/priceContractCache.spec.ts`
- `ui.laso/src/lib/__tests__/posPriceContracts.spec.ts`
- `ui.laso/src/lib/__tests__/localDbMigrationV38.test.ts`
- `ui.laso/src/lib/__tests__/localDbPrescriptionLicenseOptional.test.ts`
- `ui.laso/src/lib/__tests__/liveBackfillReplay.test.ts`
- `ui.laso/src/hooks/__tests__/useCart.test.tsx`
- `ui.laso/src/pages/__tests__/POSPage.offline.test.tsx`
