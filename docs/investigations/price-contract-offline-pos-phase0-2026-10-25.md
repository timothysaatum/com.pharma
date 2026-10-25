# Offline price-contract fix: Phase 0 verification

**Date:** 2026-10-25
**Branch / tip:** `fix/customers-sync` @ `2623202`
**Scope:** Phase 0 only. Verify the online contract path, reproduce the offline failure, stop the backend, and answer the four Phase 0 questions with code evidence. No source code, schema, migration, or production data was changed.
**Basis:** `docs/investigations/price-contract-offline-pos-2026-10-24.md`

Claims are **VERIFIED** when observed at runtime or read directly from source. **INFERRED** means reasoned from verified evidence.

---

## 1. Verdict

The failure reproduced in the running app: with the backend stopped and the POS remounted, the contract picker showed `— Select contract —`, the issue band showed a retry action, and `Complete Sale` was disabled. The local `price_contracts` cache was empty, so the offline load returned no contracts.

The online path worked: the backend returned the real `Standard Pricing` contract, and the POS auto-selected it and enabled checkout.

One correction to the assumed server behavior: the `/sync/events` `SaleProjector` does **not** validate, reject, or re-price a contract. It copies whatever contract fields are present in `sale_created`. A missing contract becomes `NULL`; the organization default is **not** applied on that path.

---

## 2. Runtime evidence

### 2.1 Backend startup

The backend was started from the project root with the project virtualenv active:

```
cd /home/vermithor/Desktop/inventory/com.pharma
source /home/vermithor/lasoenv/bin/activate
./start_backend.sh
```

Observed:

- `GET /health` returned `200` with `"status":"ok"`.
- The app was already authenticated as `tim` / `Richard Saatum`, branch `Berekum HFH`, and was polling `GET /sync/events` and `GET /conflicts` successfully.
- `GET /api/v1/contracts/available/521ef6b1-437f-4cd5-878d-59d51902871e` returned `200` with:
  - id `9ce66829-e694-4d2e-b16d-dedabecd214c`
  - name `Standard Pricing`
  - type `standard`
  - default `true`
  - verification `false`
  - approval `false`

### 2.2 Online POS

The POS page was opened through the app UI. It called the available-contracts endpoint and the contract picker contained `Standard Pricing (Standard)`. After adding Paracetamol 500mg, `Complete Sale · ₵5.00` was enabled.

**VERIFIED**, from the backend access log and the accessibility tree of the running app.

### 2.3 Backend stopped

The backend process group was terminated. Port `8000` stopped listening and `/health` returned connection refused. The POS was navigated away and back, then Paracetamol 500mg was added.

Observed in the running app:

- `Price contract` combo box: `— Select contract —`
- issue notification present with a `RETRY` button
- `Complete Sale · ₵5.00` present but not enabled
- no local contract options

This is the failing behavior. The exact copy `Offline, and no price contract is stored on this device for this branch` is gated on `navigator.onLine === false`. In this run the machine still had a network connection, so the app took the backend-unreachable branch and used the same empty-local-cache result. The sale still could not proceed.

**VERIFIED.** The exact offline copy was not shown because the network interface was not disabled; that would require host-level network changes outside Phase 0.

### 2.4 Recovery

The backend was restarted and the POS reloaded. The app again fetched `GET /contracts/available/...`, showed `Standard Pricing (Standard)`, and enabled `Complete Sale`. The cart line was removed and the app was left on Reports.

**VERIFIED.**

### 2.5 Final runtime state

- Backend stopped: port `8000` has no listener.
- Tauri app remains running, logged in, on Reports.
- No application code changed.
- No migration changed.
- No production endpoint was contacted.
- The access token used for direct read-only API verification was deleted from `/tmp`.

---

## 3. Phase 0 question a: what `/sync/events` does with contract fields

**Route:** `POST /sync/events` dispatches through `ProjectorRegistry` to `SaleProjector`.

**Validation:** `backend.laso/app/services/sync/eventlog/projectors/sale.py:90-155` validates:

- `organization_id` is present and matches the envelope
- `sale_number`
- `cashier_id`
- `payment_method`
- `payment_status`
- `status`
- at least one item, with `item_id`, `drug_id`, `drug_name`, and positive integer `quantity`

It does **not** check:

- `price_contract_id`
- contract existence, ownership, active status, effective dates, branch applicability, role applicability
- verification or approval requirements
- whether the payload prices match the selected contract

**Apply:** `backend.laso/app/services/sync/eventlog/projectors/sale.py:204-277` inserts the sale and copies:

```
price_contract_id: _uuid_str_or_none(payload.get("price_contract_id"))
contract_name: payload.get("contract_name")
contract_discount_percentage: payload.get("contract_discount_percentage")
contract_type: payload.get("contract_type")
```

Missing values become `NULL`. No default lookup happens. `use_server_fefo` and stock allocation run later, but they do not revalidate or re-price the contract.

**Rejected state:** no contract field produces `REJECTED_PERMANENT` in this path. It produces a sale with null contract columns.

**Consequence:** a client that completes an offline sale without a contract would be accepted by `/sync/events` with null contract data. Step B cannot be satisfied by client changes alone if the requirement is “server applies the default on sync.”

---

## 4. Phase 0 question b: could server contract events work?

`ServerEventEmitter` exists at `backend.laso/app/services/sync/eventlog/server_emitter.py`. It can append a server-authored event with the org lock and hash chain, and its exceptions are logged and swallowed so the primary REST write is not rolled back.

Existing users:

- `customer_endpoints.py`
- `drug_endpoints.py`

Contract endpoints do not call it, and there is no `PriceContract` projector in `backend.laso/app/services/sync/eventlog/projectors/__init__.py`.

A server projector is **not required** for server-authored contract writes: the REST endpoint already wrote the contract row, so a projector would be redundant for server state. It would be required if the server accepted client-pushed `price_contract_*` events. The device does have local contract handlers, but they are currently unreachable because no such events exist.

The existing backfill does not use `ServerEventEmitter`; it appends `price_contract_created` directly through the event-log append path. Server-originated events are therefore technically possible without a server projector.

---

## 5. Phase 0 question c: offline filtering of verification/approval contracts

`ui.laso/src/lib/localRead.ts:887-915` filters only:

- `is_deleted = 0`
- `is_active = 1`
- `status = 'active'`
- effective date window
- branch applicability
- mandatory `organization_id`

It does **not** filter `requires_verification`, `requires_approval`, `requires_preauthorization`, or `type = 'insurance'`.

POS behavior:

- `POSPage.tsx:325-333` computes `requiresOnlineContractVerification` from the selected contract.
- `POSPage.tsx:562-570` throws `"This contract requires online verification before checkout."` only in the offline checkout path.
- The online path calls `contractsApi.verifyEligibility` and supplies the server token.

So the server does not reject the contract selection on `/sync/events`; the block is a client-side offline guard. The user’s premise that the server would permanently reject such a sale is false for the event path. Any server-side enforcement added later must be designed explicitly.

---

## 6. Phase 0 question d: contract backfill

`backend.laso/scripts/backfill_stock_events.py` already supports contract backfill.

Verified details:

- It queries non-deleted contracts for the target org.
- It plans `price_contract_created` events with deterministic IDs.
- The payload mirrors the contract row field for field.
- Contracts are emitted before their dependent records.
- Dry-run is the default; applying requires `--apply --i-have-a-backup`.
- The default org/branch is the production target and must not be run without a backup and explicit authorization.
- It only creates missing create events. It does not emit updates, deletes, suspensions, activations, or approvals.

Does the REST path make a backfill unnecessary? Today, no: the device pulls only events and does not persist the online `/contracts/available` response. A Phase 2 hydration step would remove the need for an initial cache backfill on devices that successfully hydrate, but it would not replace ongoing event propagation for other devices or future contract changes.

---

## 7. Production code check

Code-only, no production request was made.

- `ui.laso/.env.production` sets `VITE_API_URL=https://api.atlas.drive4health.org`.
- The packaged app uses the same `contractsApi`, `localRead`, `localWrite`, and sync code as the development app.
- The same missing contract event path and same empty local cache behavior are present in the production code path.
- Live production data was not inspected. The observation remains code-level, not a production reproduction.

The backfill script’s default target is the production org. It was not run.

---

## 8. Phase 1 decision points

These must be resolved in the Phase 1 plan before any code is written.

1. **Server default on `/sync/events`.** The event projector currently stores `NULL` for a missing contract. If the requirement is “offline sale with no contract is accepted and the server applies the org default,” Phase 2 must add that resolution to the event path, not only to `POST /sales`. `sale_validators.py:193-225` already has default lookup logic for the online POST path.

2. **Server contract validation on `/sync/events`.** There is none today. Adding default resolution is a natural place to decide whether to validate contract existence, status, dates, branch, and verification requirements. Without an explicit decision, the event path remains more permissive than the online POST path.

3. **Verification-required contracts offline.** The plan must hide or block them at selection time, not only after the user clicks checkout. The server token requirement should remain.

4. **Hydration scope.** The plan needs to define when `/contracts/available/{branch_id}` is persisted: login, branch switch, reconnect, periodic sync, or all of them, and how stale contracts are removed per branch.

5. **Backfill.** If hydration is implemented, decide whether a one-time contract backfill is still needed for devices that hydrate. Never run the production backfill without a backup and explicit approval.

---

## 9. Files read in Phase 0

| Concern | File |
|---|---|
| POS contract load, offline state, verification block | `ui.laso/src/pages/POSPage.tsx` |
| Contract picker, auto-select, disabled checkout | `ui.laso/src/components/pos/CartPanel.tsx` |
| Local contract query | `ui.laso/src/lib/localRead.ts` |
| Sale envelope | `ui.laso/src/lib/localWrite.ts` |
| Local sale/contract projectors | `ui.laso/src/lib/localProjectors.ts` |
| Offline sale wrapper | `ui.laso/src/lib/offlineSalesManager.ts` |
| Online contract API | `ui.laso/src/api/contracts.ts` |
| API base URL and offline flag | `ui.laso/src/api/client.ts` |
| Sale projector | `backend.laso/app/services/sync/eventlog/projectors/sale.py` |
| Server event emitter | `backend.laso/app/services/sync/eventlog/server_emitter.py` |
| Projector registry | `backend.laso/app/services/sync/eventlog/projectors/__init__.py` |
| Event router | `backend.laso/app/services/sync/eventlog/router.py` |
| Contract endpoints | `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py` |
| Contract service and POS query | `backend.laso/app/services/contracts/price_contract_service.py` |
| Online sale default contract | `backend.laso/app/services/sales/validators/sale_validators.py` |
| Onboarding seed | `backend.laso/app/services/org/organization_onboarding_service.py` |
| Contract backfill | `backend.laso/scripts/backfill_stock_events.py` |
| Production API URL | `ui.laso/.env.production` |
