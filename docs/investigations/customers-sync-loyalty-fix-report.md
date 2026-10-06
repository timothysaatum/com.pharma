# Customers sync and loyalty fix — final report

**Branch:** `fix/customers-sync` @ `2987bcf` (from `feat/rx-sync-and-forms` @ `88162ed`)
**Worktree:** `/tmp/pharmacare-fix/wt`
**Date:** 2026-10-06

> **Note on the worktree:** the `/tmp/pharmacare-fix` directory was deleted from disk partway through (external `/tmp` cleanup — not my cleanup command, which was scoped to `pgdata`/`pgsock`/`*.out`/`pg.log`). Git still held the branch, so **no commits were lost**. The stale worktree registration was pruned and the worktree re-added at `2987bcf`, verified clean.

---

## 1. Findings that changed the design

**1.1 The misattribution was latent, not live — so I hardened rather than "fixed a breach".**
The investigation's headline was a permanent cross-tenant misattribution. Re-checking atlasdb: Kwame has **10** `customer_created` events, and in **every one**, `envelope.org_id == payload.organization_id`. The mismatch in the report was synthetic — I had constructed it in a harness. The finding that did change the design is worse in a different way: an event whose envelope *and* payload both name a non-existent org **passes** `CustomerProjector._validate_created`, because that function only compares the payload against the envelope (`backend.laso/app/services/sync/eventlog/projectors/customer.py:164-169`). Only the `customers_organization_id` FK stops it. So the device was the weak side, and I added the same check there.

**1.2 The device never had a snapshot writer at all.**
I expected a bootstrap path that copies server customers onto the device. There is none. The only writers of the device `customers` table are `_customerCreated` (`localProjectors.ts:208`), `_customerUpdated` (`:270`), `_customerDeleted` (`:287`) and `writeLocal.customer`. This changed the Phase 3 answer: **no amount of ordinary re-pulling can repair a device customer row**, because a pull carries only events, and the correct-org event for Kwame sits at seq 25 while the cursor is past 100. Hence the scoped re-pull in §7.

**1.3 The device table cannot hold the order aggregates.**
`localDb.ts:431` never created `total_orders` / `total_value`; `migrate_v27` (`localDb.ts:2553-2562`) adds six *other* columns but not these; no projector references them. So Phase 3.3's "update `total_orders`/`total_value` exactly as online does" is **server-side only**. Per instruction I did not add the columns. The device's Joe row has no order count at all.

**1.4 `_customerUpdated` is a real data-loss path, and routing loyalty through it would have walked into it.**
It overwrites every field present in the payload with **no `sync_status` guard** (`localProjectors.ts:240-270`), so a server event silently discards offline edits on a pending row. The new event carries a balance and nothing else, so it gets a **dedicated handler** that touches exactly two columns. This is also the concrete reason the event is a new type rather than a `customer_updated`: the design confirmed the investigation's warning.

**1.5 The header overlap does not exist, so I changed nothing there.**
The task said to fix it "if it still overlaps". At `88162ed`, neither `CustomersPage` nor `PrescriptionsPage` has an orange pill in its header: both are a plain `flex items-center justify-between` row with no absolute positioning, and the only offline pill (`SyncIndicator`, `src/components/layout/SyncIndicator.tsx:88`) lives in the AppShell sidebar. I made no change and invented no substitute.

**1.6 Process finding — my environment claim in the first draft of this report was wrong, and Julien caught it.**
I asserted no PharmaCare backend was running and that the app talked to a dead port 8001. Both false: the backend runs as `python main.py` (pid 174977, cwd `backend.laso`, `127.0.0.1:8000`, `/health` → 200), and Vite resolves `.env.local` (8000) over `.env` (8001), so app and backend agree. I had found a containerised uvicorn, assumed it was the only one, and never checked for `main.py`. Corrected in `2987bcf`.

---

## 2. Commits and files

Ten implementation commits, plus this document as the eleventh.

| Hash | Subject | Files changed |
|------|---------|---------------|
| `41a72a8` | `fix(customers): fail closed on unscoped customer reads and cross-org creates` | `ui.laso/src/lib/localRead.ts`<br>`ui.laso/src/lib/localProjectors.ts`<br>`ui.laso/src/pages/PrescriptionsPage.tsx`<br>`ui.laso/src/lib/__tests__/customersSyncPhase1.test.ts` *(new)*<br>**4 files, +286 −12** |
| `3d8a7c1` | `fix(customers): sweep foreign-org customers and extend pending-marker repair` | `ui.laso/src/lib/localDb.ts`<br>`ui.laso/src/stores/authStore.ts`<br>`ui.laso/src/lib/__tests__/foreignCustomerCleanup.test.ts` *(new)*<br>`ui.laso/src/lib/__tests__/stalePendingMarker.test.ts` *(new)*<br>**4 files, +534 −1** |
| `724f230` | `feat(sync): optional scoped one-shot re-pull for a single aggregate type` | `ui.laso/src/api/sync.ts`<br>`ui.laso/src/lib/syncEngine.ts`<br>`ui.laso/src/lib/__tests__/scopedRepull.test.ts` *(new)*<br>**3 files, +199 −2** |
| `502629d` | `feat(customers): publish customer_loyalty_changed so devices converge on loyalty` | `backend.laso/app/services/sales/sales_service.py`<br>`backend.laso/app/services/sales/utils/loyalty.py` *(new)*<br>`backend.laso/app/services/sync/eventlog/loyalty_event_id.py` *(new)*<br>`backend.laso/app/services/sync/eventlog/projectors/customer.py`<br>`backend.laso/app/services/sync/eventlog/projectors/sale.py`<br>`backend.laso/tests/integration/test_loyalty_convergence.py` *(new)*<br>`backend.laso/tests/unit/test_loyalty_event_id_vectors.py` *(new)*<br>`ui.laso/src/lib/loyaltyEventId.ts` *(new)*<br>`ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts` *(new)*<br>`docs/decisions/0010-customer-loyalty-event-contract.md` *(new)*<br>**10 files, +1533 −23** |
| `4a8a282` | `feat(customers): device applies customer_loyalty_changed without touching local edits` | `ui.laso/src/lib/localProjectors.ts`<br>`ui.laso/src/pages/CustomersPage.tsx`<br>`ui.laso/src/components/pos/CartPanel.tsx`<br>`ui.laso/src/lib/__tests__/customerLoyaltyProjector.test.ts` *(new)*<br>**4 files, +282** |
| `24b5d26` | `feat(customers): loyalty backfill script, dry-run by default` | `backend.laso/scripts/backfill_customer_loyalty_events.py` *(new)*<br>**1 file, +380** |
| `3a1635d` | `test(scripts): fence the two E2E seed scripts against a real database` | `backend.laso/scripts/refuse_real_database.py` *(new)*<br>`backend.laso/scripts/seed_bulk_e2e.py`<br>`backend.laso/scripts/seed_test_catalog.py`<br>`backend.laso/tests/unit/test_refuse_real_database.py` *(new)*<br>**4 files, +238** |
| `9cfc35a` | `test(customers): S-A..S-H investigation scenarios as a standing suite` | `ui.laso/src/lib/__tests__/customersSyncScenarios.test.ts` *(new)*<br>`ui.laso/src/lib/__tests__/localDbPrescriptionLicenseOptional.test.ts`<br>**2 files, +286 −2** |
| `db19dbd` | `fix(scripts): import the database fence in a way that works both ways` | `backend.laso/scripts/seed_bulk_e2e.py`<br>`backend.laso/scripts/seed_test_catalog.py`<br>**2 files, +11 −2** |
| `2987bcf` | `docs(customers): correct the owner steps - the backend runs as python main.py` | `docs/investigations/customers-sync-loyalty-fix-report.md`<br>**1 file, +72 −43** |

**No Alembic revision is part of this change.** The only schema change is the device-side SQLite migration v37, applied by the app itself.

---

## 3. The event contract and the decision record

### 3.1 The event

**Type:** `customer_loyalty_changed`
**`aggregate_type`:** `customer` · **`aggregate_id`:** the customer id

Payload fields:

| Field | Type | Meaning |
|---|---|---|
| `customer_id` | uuid string | the customer |
| `organization_id` | uuid string | must equal `envelope.org_id`; rejected as `org_scope_violation` otherwise |
| `loyalty_points` | int ≥ 0 | **absolute** post-state balance |
| `loyalty_tier` | `bronze\|silver\|gold\|platinum` | must equal `resolve_loyalty_tier(loyalty_points)`; rejected as `loyalty_tier_inconsistent` otherwise |
| `sale_id` | string | the sale that caused it (id or number) |
| `direction` | `earn\|refund` | in the id seed; anything else rejected as `invalid_direction` |
| `source` | string | `online_sale` \| `offline_sale` \| `refund` \| `backfill` |
| `sale_number` | string, optional | human-readable |

**Absolute, not a delta** — a duplicate is an *assignment*, not an addition.

### 3.2 The exact event-id derivation rule

```
seed     = "customer_loyalty_changed|<customer_id>|<sale_id>|<direction>"
digest   = sha256(seed)
event_id = digest[:26].upper()
```

**Server helper — `backend.laso/app/services/sync/eventlog/loyalty_event_id.py:47-64`**

```python
39:  EVENT_TYPE = "customer_loyalty_changed"
42:  DIRECTION_EARN = "earn"
43:  DIRECTION_REFUND = "refund"
44:  DIRECTIONS = (DIRECTION_EARN, DIRECTION_REFUND)

47:  def customer_loyalty_changed_event_id(
48:      customer_id: Any, sale_id: Any, direction: str
49:  ) -> str:
        if direction not in DIRECTIONS:
            raise ValueError(...)
        return deterministic_event_id(
            EVENT_TYPE, str(customer_id), f"{sale_id}|{direction}"
        )
```

It delegates to **`backend.laso/app/services/sync/eventlog/stock_emitter.py:57-77`** — `deterministic_event_id(event_type, aggregate_id, version_tag)` — the *same* helper the prescription refill id and the stock backfills already use, so the 26-character `ULID_LENGTH` uppercase-hex rule is identical across every deterministic event in the system.

**Device helper — `ui.laso/src/lib/loyaltyEventId.ts:44-57`**

```typescript
26:  const ULID_LENGTH = 26;
31:  export const LOYALTY_EVENT_TYPE = "customer_loyalty_changed";

44:  export async function customerLoyaltyChangedEventId(
45:      customerId: string, saleId: string, direction: LoyaltyDirection,
46:  ): Promise<string> {
        if (!LOYALTY_DIRECTIONS.includes(direction)) {
            throw new Error(...);
        }
        const seed = `${LOYALTY_EVENT_TYPE}|${customerId}|${saleId}|${direction}`;
        const digest = await sha256Hex(seed);
        return digest.slice(0, ULID_LENGTH).toUpperCase();
    }
```

SHA-256 via `crypto.subtle`, falling back to `node:crypto` so it is testable under vitest without adding a dependency.

**Pinned to identical vectors on both sides** — `backend.laso/tests/unit/test_loyalty_event_id_vectors.py` and `ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts`. A change to either formula fails both suites.

| customer_id | sale_id | direction | event_id |
|---|---|---|---|
| `5823ef27-51ce-4431-9adc-e81f9b3f949f` | `APO1-20261005-0001` | `earn` | `EB25262A7317448712B80292AA` |
| `5823ef27-51ce-4431-9adc-e81f9b3f949f` | `APO1-20261005-0001` | `refund` | `DB76F8DFBAA917956EB45A8074` |
| `0615fb0e-416f-4ecb-bca1-d4839446cb36` | `APO1-20261005-0001` | `earn` | `831785162E51C425BCD33CB87F` |
| `5823ef27-51ce-4431-9adc-e81f9b3f949f` | `99999999-8888-7777-6666-555555555555` | `earn` | `69F3506D0779E344454F6CEF75` |

### 3.3 Ordering

Emitted **inside the same transaction, after the points are written**, through `StockEventEmitter.emit_in_transaction` (`stock_emitter.py:91-108`), which explicitly *does not swallow errors* so the caller must let them propagate. If the points commit, the event is in the log; if the append fails, the transaction rolls back the points.

- Online sale: `sales_service.py` step 19 mutates, step 19b emits, immediately after the `sale_created` emit.
- Refund: emits at step 7b, before the commit at step 8.
- Offline: emits from inside `SaleProjector._apply_offline_loyalty`, after the award — bending ADR 0007 exactly as `prescription_refill_used` already does.

**Server `sync_status`.** `process_sale` calls `customer.mark_as_synced()` after a successful append. Previously the row went to `pending` with no event behind it — Joe's exact state (`sync_status=pending`, `sync_version=5`). There is no server-side reconciler for its own emitted events, so marking at the emit site is the consistent choice and mirrors the device-side reconciliation added in `88162ed`.

### 3.4 Replay behaviour

| Situation | Result |
|---|---|
| Same event delivered twice | Absolute value → an assignment. No drift. Idempotent. |
| `sale_created` re-projected | `ON CONFLICT (id) DO NOTHING … RETURNING id` returns `None`, so the function returns before reaching the award block (`projectors/sale.py:256-257`, `304-306`). Nothing awarded twice. |
| Earn then refund | Two distinct ids. The refund is not swallowed as a duplicate. Balance returns to where it started. |
| Refund seen before its earn | Both absolute; the last applied wins on a real server value. No counter can go negative. |
| Event for a customer the device does not hold | Raises → `syncEngine` calls `recordEventProjectionFailure`, then advances past. The row is **never created**. |
| Tier contradicting the points | Server: `REJECTED_PERMANENT` / `loyalty_tier_inconsistent`. Device: quarantined after 3 attempts. |
| Cross-org loyalty event | Server: `org_scope_violation`. Device: refuses, matching id **and** org. |

### 3.5 Why duplicates are safe

Three independent reasons, so no single one has to hold:

1. **The payload is absolute.** A duplicate is `SET loyalty_points = 125`, not `+ 30`. A device that missed an event converges on the next one instead of drifting.
2. **The id is derived from the change's identity.** `event_log`'s primary key is `(org_id, event_id)`, and the append path returns `ALREADY_APPENDED` for a repeated id — so the duplicate is refused before a projector ever runs.
3. **The server is the only writer of the balance.** The device never computes loyalty; it copies one.

### 3.6 The decision record, in full

`docs/decisions/0010-customer-loyalty-event-contract.md`:

````markdown
# 0010: Customer Loyalty Event Contract

**Status:** Accepted
**Date:** 2026-10-06
**Related:** [0006 — Event-Sourced Sync Spine](0006-event-sourced-sync-spine.md),
[0007 — Event Schema, Hash Chain, and Dependency Semantics](0007-event-schema-hash-chain-dependencies.md),
`app/services/sync/eventlog/refill_event_id.py`

## Context

Loyalty points were awarded on exactly one code path: `SalesService.process_sale`,
the online sale. Three gaps followed from that, all confirmed by execution against
a disposable cluster and by reading `atlasdb`:

1. The offline path never awarded anything. `SaleProjector` creates the `Sale` and
   its items and stops; it does not touch `customers`. A sale taken offline and
   synced later produced a sale row with no points behind it.
2. No event was ever emitted for a loyalty change. `customers.loyalty_points` is a
   bare counter mutated in place, so the device had nothing to apply and no way to
   learn the new balance. Joe's server row sat at `sync_status='pending'`,
   `sync_version=5`, with no event behind it and a device row still showing 0.
3. The device had no loyalty read or write path at all: the `sale_created` payload
   carries no loyalty keys, and no projector touches `customers`.

A fourth, adjacent fact shaped the design: a refund reverses `loyalty_points` and
re-resolves the tier, but leaves `total_orders` and `total_value` untouched. That
inconsistency is reported, not fixed, here.

## Decision

### 1. The event

A new event type, `customer_loyalty_changed`, carrying the customer's **absolute**
post-state:

```json
{
  "customer_id": "<uuid>",
  "loyalty_points": 125,
  "loyalty_tier": "silver",
  "sale_id": "<sale id or number>",
  "direction": "earn" | "refund"
}
```

`aggregate_type` is `customer` and `aggregate_id` is the customer id, so the event
flows through the existing customer projector and the existing
`AGGREGATE_TABLES` reconciliation.

**Why absolute rather than a delta.** An absolute value makes a duplicate
assignment rather than an addition: applying the same event twice lands on the
same number. A delta would need the device to be exactly-once, which is precisely
the property we do not have. Absolute also means a device that missed an event
converges on the next one, instead of drifting.

### 2. The event id

```
sha256("customer_loyalty_changed|<customer_id>|<sale_id>|<direction>")
  -> first 26 characters, uppercased
```

Implemented once in `app/services/sync/eventlog/loyalty_event_id.py`, mirroring
`ui.laso/src/lib/loyaltyEventId.ts`. Both sides reuse
`deterministic_event_id` (`stock_emitter.py:55`), the same helper the refill id and
the backfills use, so the 26-character `ULID_LENGTH` rule is identical across every
deterministic event in the system.

**`direction` is in the seed, and that is the load-bearing part.** One sale's earn
and that sale's refund must not share an id. If they did, the refund would be
swallowed by `event_log`'s `(org_id, event_id)` primary key as a duplicate of the
earn, and the customer would keep the points they had just given back. The id
helper raises on an unknown direction rather than deriving an id that could
collide.

The two implementations are pinned to the same four vectors by
`tests/unit/test_loyalty_event_id_vectors.py` and
`ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts`. A change to either formula
fails both suites.

### 3. Ordering

The event is emitted **inside the same transaction** as the points update, after
it, through the non-swallowing server emitter. If the points commit, the event is
in the log; if the transaction rolls back, neither happened. Emitting before the
update would let a device observe a balance the server then failed to write.

The offline path follows the precedent set by `prescription_refill_used`: the
projector emits from inside `apply`. ADR 0007 asks projectors to be pure functions
of (event, read model); that is already bent for the refill event, and the
deterministic id is what makes it safe — a replay of the same `sale_created`
re-derives the same id, the append returns `ALREADY_APPENDED`, and the re-emit is a
no-op. Loyalty does not bend it further than that.

### 4. Replay behaviour

| Situation | Result |
|---|---|
| Same event delivered twice | Second `apply` writes the same absolute value. No drift. |
| `sale_created` re-projected | `ON CONFLICT (id) DO NOTHING` on the sale, and the same derived event id, so no second award. |
| Earn then refund | Two distinct ids, two events, balance returns to where it started. |
| Out-of-order (refund seen before its earn) | Both are absolute, so the last one applied wins and the row holds a real server value. There is no counter to go negative. |
| Event for a customer this device does not have | Recorded as a projection failure; **not** silently inserted. |

### 5. Why duplicates are safe

Three independent reasons, so no single one has to hold:

1. The payload is absolute, so a duplicate is idempotent by construction.
2. The id is derived from the change's identity, so the server's append path
   refuses the duplicate before a projector ever runs.
3. The server's read model is the only writer of `loyalty_points`. The device
   never computes a balance; it copies one.

### 6. What this ADR does not decide

- **No points ledger.** `loyalty_points` remains a counter on the customer row. A
  ledger makes every award reconstructible and is the right long-term answer; it is
  a separate project with its own migration.
- **Refund and `total_orders` / `total_value`.** A refund currently reverses points
  and the tier but not the order count or the spend total. Options are in the
  investigation report; changing it here would alter historical aggregates on data
  nobody has audited.
- **Redemption.** Not implemented. The thresholds exist, the ledger does not.
````

---

## 4. Tests before / after

### 4.1 Counts

| Suite | Before | After | Delta |
|---|---|---|---|
| **UI — test files** | 73 passed | **80 passed** | **+7** |
| **UI — tests** | 530 passed | **587 passed** | **+57** |
| **`tsc --noEmit`** | clean (exit 0) | **clean (exit 0)** | — |
| **Backend unit** (`tests/unit`) | 216 passed | **240 passed** | **+24** |
| **Backend integration** (`tests/integration`) | 184 passed | **203 passed** | **+19** |
| **Backend total** | 400 | **443** | **+43** |

Chunking: the backend suites were run as two chunks (`tests/unit`, then `tests/integration`), **one pytest process at a time**, against a disposable PostgreSQL cluster (initdb in `/tmp`, unix socket only, DB name `custfix`, destroyed afterwards). Cluster tuning (`fsync=off`, `synchronous_commit=off`, `full_page_writes=off`, unix socket) cut the per-test `DROP SCHEMA`/`create_all` cost from ~24 s to ~4 s. `TEST_DATABASE_URL` never pointed at atlasdb; the existing fence at `tests/conftest.py:44-60` was honoured throughout.

### 4.2 New regression tests — all pass

| File | Tests | Result |
|---|---|---|
| `ui.laso/src/lib/__tests__/customersSyncPhase1.test.ts` | 7 | **PASS** |
| `ui.laso/src/lib/__tests__/foreignCustomerCleanup.test.ts` | 6 | **PASS** |
| `ui.laso/src/lib/__tests__/stalePendingMarker.test.ts` | 9 | **PASS** |
| `ui.laso/src/lib/__tests__/scopedRepull.test.ts` | 5 | **PASS** |
| `ui.laso/src/lib/__tests__/loyaltyEventId.spec.ts` | 8 | **PASS** |
| `ui.laso/src/lib/__tests__/customerLoyaltyProjector.test.ts` | 9 | **PASS** |
| `ui.laso/src/lib/__tests__/customersSyncScenarios.test.ts` | 13 | **PASS** |
| `backend.laso/tests/integration/test_loyalty_convergence.py` | 19 | **PASS** |
| `backend.laso/tests/unit/test_loyalty_event_id_vectors.py` | 9 | **PASS** |
| `backend.laso/tests/unit/test_refuse_real_database.py` | 15 (13 defs, one ×3 params) | **PASS** |
| **Total** | **100** | **all pass** |

### 4.3 The S-A..S-H scenarios

`ui.laso/src/lib/__tests__/customersSyncScenarios.test.ts`, real `installRealDb()`:

| ID | Scenario | Result |
|---|---|---|
| **S-A** | customer created online, then pulled → lands with the server's absolute state (125/silver, `synced`) | **PASS** |
| **S-B** | device-created customer (Mike's exact state) → `pending`, then `synced` once the event is accepted | **PASS** |
| **S-C** | online sale's points reach the device on the next pull: 0 → 125/silver | **PASS** |
| **S-D** | offline-synced sale earns exactly once; three replays leave it at 30 | **PASS** |
| **S-E** | refund reverses to 0/bronze; the earn and refund ids differ | **PASS** |
| **S-F** | duplicate ×3 → stays 90; refund-before-earn → lands on 125 | **PASS** |
| **S-G** | unknown customer → recorded `sync_event_failures` row, cursor not frozen; two devices converge on the same absolute state | **PASS** |
| **S-H** | cross-org same-id collision refused (foreign row untouched); **pending row with local edits NOT clobbered** (`JoeLOCAL` preserved, `sync_status` still `pending`) | **PASS** |

Backend-side loyalty scenarios (`test_loyalty_convergence.py`, real Postgres): shared formula; tier invariant; disabled-org no-op; online award + event + `synced` row; offline award once; offline replay awards nothing more; walk-in earns nothing; **online and offline formulas cannot drift**; refund restores and publishes the opposite direction; refund ids differ; refund does **not** reverse counters (pinned); projector accepts a consistent state; rejects tier/points contradiction, cross-org, negative points, bad direction; apply writes only loyalty columns; apply idempotent; apply never creates a missing customer.

### 4.4 Two pre-existing tests I had to correct

- `localDbPrescriptionLicenseOptional.test.ts` asserted the migration chain stopped at exactly **36**. It now asserts the chain reached its current head (**37**) — which is what the test was actually for, since v37 is the cross-org cleanup.
- The fence import broke `test_catalog_seed_service.py` collection (`ModuleNotFoundError: refuse_real_database`), because that test imports `from scripts.seed_test_catalog import …`. Fixed in `db19dbd` by trying the package path first and falling back to the script's own directory.

### 4.5 One pre-existing contamination I did **not** fix

`backend.laso/tests/integration/test_stock_event_device_parity.py:57` **writes** the tracked UI fixture `ui.laso/src/lib/__tests__/fixtures/stock-parity.json`. Every backend integration run therefore leaves the repo dirty and races the UI suite on that file (I also saw `stock-backfill-parity.json` dirtied). Reproduced twice, reverted both times. Fixing it means deciding where the fixture lives and who owns it — a cross-tree contract change, not a small gap.

---

## 5. Loyalty backfill dry-run table — as printed

Run against **atlasdb**, `READ ONLY` session, **nothing written**. `--apply` was **never** run against atlasdb; it was exercised only against the disposable cluster.

```
backfill_customer_loyalty_events.py — DRY-RUN (default)
org 2d060ef8-a302-447c-91f4-b2fd30268341  author bae475d9-994a-4d5b-abb2-32aa4b082602
version loyalty-backfill-v1

org head seq before: 104

customer                   name            points tier      orders  action
--------------------------------------------------------------------------
5823ef27-51ce-4431-9adc-e81f9b3f949f Joe                125 silver         4  emit A0A253116B80FF11C0126E1084
0615fb0e-416f-4ecb-bca1-d4839446cb36 Mike                 0 bronze         0  emit 0E0DE67C4B5824417FCED49A0E

to emit: 2   already present: 0   skipped: 1
  88888888-8888-8888-8888-888888888888: skipped - owner decision pending

Dry run. Nothing written. Re-run with --apply --i-have-a-backup to append.
```

**Seq range:** the org head is at seq **104**; these two events would be appended at **seq 105 and 106**. Nothing else is emitted — the backfill publishes exactly one event per customer, carrying current absolute state.

**Idempotency, verified on the disposable cluster:** first `--apply` → `appended 2  already present 0  failed 0`; second `--apply` → `to emit: 0  already present: 2`, `appended 0`. The event ids printed are byte-identical across both the disposable run and atlasdb, confirming determinism.

**Guard, verified:** `--apply` without `--i-have-a-backup` prints `REFUSING: --apply requires --i-have-a-backup.` and exits 2 with nothing written.

**Kwame Nkrumah** is **not deleted and not modified**; printed as `skipped - owner decision pending`. The script additionally refuses to publish any row whose stored tier contradicts `resolve_loyalty_tier(points)`, so a bad row is reported rather than pushed onto every device.

---

## 6. The device migration (v37)

### 6.1 What it changes

`ui.laso/src/lib/localDb.ts` — `migrate_v37` + `repairCrossOrgCustomers`, registered at `localDb.ts:307` (`if (user_version < 37) await migrate_v37(db);`). Follows the `repairCrossOrgPriceContracts` pattern (`localDb.ts:2969`) exactly: the migration itself is a **no-op during the schema chain** (the signed-in org is not known yet), and the work happens in `repairCrossOrgCustomers(organizationId)`, called from `authStore.ts` `login`.

It deletes `customers` rows where `organization_id IS NULL OR organization_id <> $1`, **except** any row whose id still appears in `event_outbox` for `aggregate_type='customer'` with status in `('pending','failed','accepted_deferred')`. `'synced'` means already delivered, so those are safe to remove. Returns the count for `console.info`. Non-fatal, idempotent, never blocks sign-in.

**Why delete rather than relabel:** relabelling would hand another tenant's customers to this pharmacy, which is strictly worse than having none. Rows for another org are useless here; if that org is ever signed in on this device again, its customers return with the next sync.

### 6.2 How it was tested

`ui.laso/src/lib/__tests__/foreignCustomerCleanup.test.ts` — 6 tests, all **PASS**, over real `installRealDb()`:

| Test | Fixture | Assertion |
|---|---|---|
| **49 foreign rows** | 49 rows under `11111111-…` + Joe and Kwame under `2d060ef8-…` | `repairCrossOrgCustomers(ORG)` returns **49**; exactly 2 rows remain (both the real org's) |
| **same-id collision (the Kwame case)** | `88888888-…` seeded under `11111111-…` | cleanup returns 1; the row is gone; **and the real-org `customer_created` (seq 25) then inserts correctly** — org `2d060ef8-…`, 150/gold. This is the proof that the cleanup unblocks the event that was previously swallowed. |
| **foreign row with an unsent event** | `88888888-…` under the foreign org + an outbox row `pending` | cleanup returns **0**; the row is **kept** |
| **every unsent status** | 3 foreign rows with `failed`, `accepted_deferred`, `synced` | returns 1 — the `synced` one is removed, `failed` and `accepted_deferred` are kept |
| **idempotent** | one foreign row | 1, then 0, then 0 |
| **current org never touched** | Joe `pending`, no unsent events | returns 0; row still present and still `pending` |

### 6.3 Counts it would remove on this device

From the investigation's device census: **51 `customers` rows total — 49 under `11111111-…`, Joe and Mike under `2d060ef8-…`**.

- `repairCrossOrgCustomers(2d060ef8-…)` should report **49** removed.
- Joe and Mike are the current org's, so they are never candidates.
- Caveat: any of the 49 carrying an unsent `customer` outbox event would be **kept**, so the true count is `49 − (foreign rows with unsent work)`. The console line prints the real number: `[auth] cross-org customer cleanup: removed N row(s)`.
- Kwame's row is among the 49 and **will be deleted from the device**. That is what the cleanup is for. His **server** row is untouched, and he remains skipped by the backfill.
- After the cleanup, the real-org Kwame event (seq 25) will **not** re-arrive on its own, because the cursor is past it. `repullAggregateOnce("customer", orgId)` (§7 step 6) forces it without moving the cursor.

---

## 7. Owner steps

### Step 0 — what is actually running (verified, not assumed)

The PharmaCare backend **is** running, as `python main.py`:

```
$ pgrep -af main.py
174977 python main.py
$ readlink /proc/174977/cwd
/home/vermithor/Desktop/inventory/com.pharma/backend.laso
$ ss -lntpH 'sport = :8000'
LISTEN 127.0.0.1:8000  users:(("python",pid=175371),("python",pid=174977))
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
200
```

`main.py:241-255` binds `127.0.0.1:8000` with `reload = ENVIRONMENT != "production"`; `.env` sets `ENVIRONMENT=development`, so **the reloader is active** (pid 175371 is uvicorn's spawned worker). It currently serves `88162ed` from the main tree.

**The port is 8000 and the app agrees — read `.env.local`, not `.env`:**

| File | `VITE_API_URL` | Effective |
|---|---|---|
| `ui.laso/.env` | `http://127.0.0.1:8001` | no — overridden |
| `ui.laso/.env.local` | `http://127.0.0.1:8000` | **yes** |

Vite resolves `.env.local` over `.env`. App and backend agree; port 8001 is unused. The `.env` / `.env.local` disagreement is an easy trap to re-fall into.

Unrelated, so nobody "fixes" it: a Docker container `kratos_backend` (image `election-system-backend`) also runs uvicorn on `0.0.0.0:8000` inside its own network namespace. It is a different application and is **not** what serves `127.0.0.1:8000`. Leave it alone.

Also running: the Tauri dev app from the main tree (`tauri.js dev`, Vite on 1420, `target/debug/pharmacare`), currently serving `88162ed`.

### Step 1 — pg_dump and verify it

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$HOME/backups/atlasdb-$STAMP.dump"
mkdir -p "$HOME/backups"
set -a; . ./.env; set +a          # DATABASE_URL only; never echoed
pg_dump --format=custom --file="$OUT" "$DATABASE_URL"
```

Verify before relying on it:

```bash
ls -lh "$OUT"
pg_restore --list "$OUT" | grep -c 'TABLE DATA'
pg_restore --list "$OUT" | grep -E 'TABLE DATA.*(customers|event_log)'
```

Expect a non-zero count and both `customers` and `event_log` present. Zero means the dump is empty — stop.
**Rollback:** none; this only reads. If the file is bad, delete it and redo.

### Step 2 — backfill dry-run

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
set -a; . ./.env; set +a
export DATABASE_URL="${DATABASE_URL/postgresql+psycopg2/postgresql+asyncpg}"
export SECRET_KEY="dry-run-only" ENVIRONMENT=test
/home/vermithor/lasoenv/bin/python scripts/backfill_customer_loyalty_events.py
```

Compare against §5. The session is `READ ONLY`, so PostgreSQL itself refuses a write.
**Rollback:** nothing written.

### Step 3 — backfill apply (only after a verified dump)

```bash
/home/vermithor/lasoenv/bin/python scripts/backfill_customer_loyalty_events.py --apply --i-have-a-backup
```

Expect `appended 2  already present 0  failed 0`. Re-running must report `to emit: 0`.

**Rollback:** the events are additive, and harmless — the device handler writes two columns. Restoring the dump also removes them, but that is destructive to everything written since:

```bash
pg_restore --clean --if-exists --dbname="$DATABASE_URL" "$OUT"
```

Prefer leaving them in place. Only restore if you have nothing else to lose.

### Step 4 — switch the main tree (the backend reloads itself)

```bash
cd /home/vermithor/Desktop/inventory/com.pharma
git status                     # expect: only the 4 untracked docs
git fetch origin               # read-only
git checkout fix/customers-sync
git log --oneline -1           # expect 2987bcf
```

**No database migration is required** — the only schema change is the device-side SQLite v37, applied by the app. `alembic upgrade head` is harmless but unnecessary. Note `main.py` does **not** run alembic (only `start_backend.sh` does).

**No backend restart is needed.** `python main.py` runs with `reload=True` and uvicorn watches `backend.laso`, so the checkout triggers a reload. Watch for it in the terminal running `python main.py`, then confirm:

```bash
sleep 3
curl -s -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:8000/health
```

If `/health` does not answer, restart it yourself:

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
python main.py
```

Restarting the UI is recommended rather than assumed — the checkout rewrites many files at once and Vite's HMR can be left confused:

```bash
pkill -f 'tauri.js dev'
cd /home/vermithor/Desktop/inventory/com.pharma/ui.laso && pnpm tauri dev
```

**Rollback:** `git checkout feat/rx-sync-and-forms`. The reloader picks that up too; restart `python main.py` only if it does not.

**Which commit is the running app using?**

```bash
cd /home/vermithor/Desktop/inventory/com.pharma && git rev-parse --short HEAD
```

### Step 5 — log in once so the repairs run

Both repairs run inside `login`. Log out and back in on the device, then read the console:

```
[auth] cross-org customer cleanup: removed 49 row(s)
[auth] stale pending-marker repair: N row(s) (customers=N, sales=N, ...)
```

**Rollback:** one-shot and idempotent. Foreign rows return with the next sync if that org is ever signed in here again.

### Step 6 — optional: force the customer re-pull

After the cleanup deletes the foreign Kwame row, the real-org event (seq 25) will not re-arrive — the cursor is past it. Optional one-shot, **does not touch the cursor**:

```js
// DevTools console, while signed in
const { repullAggregateOnce } = await import("/src/lib/syncEngine.ts");
await repullAggregateOnce("customer", "<your org id>");
```

Expect `applied 1 envelope(s); stored cursor untouched`.
**Rollback:** it only applies events the server already holds.

### Step 7 — verification queries

Single-line, in DevTools on `laso.db` (read-only):

```sql
SELECT organization_id, COUNT(*) FROM customers GROUP BY organization_id;
```

```sql
SELECT substr(id,1,8) id, loyalty_points, loyalty_tier, sync_status FROM customers WHERE substr(id,1,8) IN ('5823ef27','0615fb0e');
```

```sql
SELECT status, COUNT(*) FROM event_outbox GROUP BY status;
```

```sql
SELECT key, value FROM sync_meta WHERE key LIKE 'event_pull_seq%';
```

Expected: only your org under the first; Joe `125/silver`, Mike `0/bronze`, no stranded `pending`; `event_pull_seq` unchanged by step 6.

Server side, after step 3:

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
set -a; . ./.env; set +a
psql "$DATABASE_URL" -c "SELECT seq, event_type, substr(aggregate_id::text,1,8), event_id FROM event_log WHERE event_type='customer_loyalty_changed' ORDER BY seq;"
```

Expect seq 105 and 106 for Joe and Mike, and **no row for `88888888`**.

---

## 8. Risks, things not fixed, open questions

### 8.1 Direct answers

**(a) Does the server `SaleProjector` now award points for offline-synced sales, and how does it avoid double awards?**

**Yes.** `SaleProjector._apply_offline_loyalty` (`projectors/sale.py:362-520`), called at `:352` from `_apply_created`.

It uses the **same helper** as the online path — `apply_loyalty_award` (`app/services/sales/utils/loyalty.py:172-247`) — so `points_per_unit` (org setting, default 1.0) and the post-discount total cannot drift between the two. It updates `loyalty_points`, `loyalty_tier`, `total_orders`, `total_value` and publishes the same `customer_loyalty_changed` event with `source: "offline_sale"`.

Double awards are prevented by **two independent gates**:

1. **The `RETURNING id` gate.** The sale insert is `ON CONFLICT (id) DO NOTHING … RETURNING id` (`:256-257`). A re-projected sale yields no row, and `_apply_created` returns at `:304-306` *before* reaching the award block. The award and its emit happen together or not at all — the event id alone would not be enough.
2. **The derived event id.** `customer_loyalty_changed_event_id(customer_id, sale_id, "earn")` is identical on every replay, so even if gate 1 were bypassed the append path returns `ALREADY_APPENDED`.

Verified: applying the same `sale_created` twice leaves points at 30, not 60, and produces exactly one event.

Walk-in sales earn nothing (no `customer_id`, and a `walk_in` row is skipped). Loyalty-disabled orgs earn nothing. **The sale is never rejected over loyalty** — the whole block is wrapped in `try/except` that logs a warning and continues, because the medicine has already left the shelf.

**(b) What happens on a device still on the old build when it receives `customer_loyalty_changed`? Is the cursor advanced, and is the event lost?**

**Yes, the cursor advances. Yes, the event is lost for that device.** Verified against `88162ed`, not reasoned about:

1. `applyEventLocally` has `default: console.warn("No projector for event_type=…")` (`localProjectors.ts:136-137` at `88162ed`). An unknown type **does not throw**.
2. Because it does not throw, `syncEngine.pullEvents` takes the **success** path (`syncEngine.ts:515-521` at `88162ed`): `lastSuccessSeq` is advanced to `envelope.seq`, and `clearEventProjectionFailure` is called.
3. The cursor is then persisted at `next_after_seq`, **past the event**.

So the only trace is one `console.warn` line. The device's Joe row stays at `0/bronze` and **will not be corrected by any later ordinary pull**, because the cursor moved past. The event is not written to `applied_events` either, but that is irrelevant — the cursor is what makes it permanent.

**This is a rollout risk, not a data-loss risk.** Nothing is corrupted; the old device simply never converges until it updates. The same is true for `customers:changed`, which that build neither emits nor subscribes to.

Recovery paths, in order of preference: update the device build, then run `repullAggregateOnce("customer", orgId)` (§7 step 6) — note that function does not exist on the old build, so this is an *after*-update step. Or, for a device that never updates, the backfill's events are equally invisible to it.

I did **not** add a server-side "hold events for old clients" gate, because that would break every other event type's delivery and was not in scope. Flagging it as the correct place to decide rollout policy.

**(c) Nothing pushed, main tree unchanged?**

- **Nothing was pushed.** `git branch -r | grep customers-sync` → no match. `fix/customers-sync` exists only locally.
- **The main working tree is still on `feat/rx-sync-and-forms` @ `88162ed`**, with its four untracked investigation docs and **both stashes intact**.
- `origin/feat/rx-sync-and-forms` is still `88162ed`; `origin/fix/stock-sync` is still `f2ef1b7`. Neither was touched.
- All work lives in the worktree `/tmp/pharmacare-fix/wt` on `fix/customers-sync` @ `2987bcf`.

### 8.2 Deliberately not fixed

1. **Refund does not reverse `total_orders` / `total_value`.** Reproduced and **pinned by a test** (`test_refund_does_not_reverse_order_counters`): points go 30 → 0 while `total_orders` stays 1 and `total_value` stays 30.0. After a refunded sale the customer keeps claiming spend that was undone. Options, for a decision:
   - *Full reversal on full refund, none on partial* — matches the money, but a partial refund then needs proportional order counting, which is ambiguous.
   - *Proportional on partial* — `round(refund_amount / sale_total)`; fractional order counts need a convention (floor? carry?).
   - *Leave it, and redefine the counters* as "orders ever rung up" rather than "orders kept" — cheapest, but the field names should then change so the meaning is honest.

   I did not change it: it rewrites historical aggregates nobody has audited.
2. **No points ledger.** `loyalty_points` is still a counter, so a lost award is unrecoverable *in principle*. Deferred by owner decision; ADR 0010 §6 records it.
3. **No redemption implementation.** Thresholds exist, ledger does not.
4. **The other `localRead` truthiness guards.** `searchDrugs`, `searchContracts` and the function at `localRead.ts:1358` all guard their org filter on truthiness — same class of bug, same exposure, out of scope. `searchCustomerMatches` was fixed because it is a customer read.
5. **`stock` aggregate cannot be reconciled.** There is no local `stock_adjustments` table at all (documented at `localWrite.ts:888-902`), so there is no row to flip. `purchase_order` *could* be added safely and was left out only as out of scope.
6. **Header overlap** — not reproducible, no change. Latent residual: neither page header's button group has `flex-wrap`, so a very narrow window could crowd the title. Not touched, because the reported symptom does not exist and I will not invent a fix.
7. **The parity fixture contamination** (§4.5).

### 8.3 What can still write to a real database

| Guard | Covers | Does **not** cover |
|---|---|---|
| `tests/conftest.py` (`e8dd90c`) | any pytest run whose `TEST_DATABASE_URL` names atlasdb/postgres/template0/template1 | scripts, Playwright, alembic |
| `ui.laso/tests/e2e/helpers/backend-db.ts` (`213b6fc`) | the Playwright `BackendDatabase` helper | anything not using that helper |
| `backend.laso/scripts/refuse_real_database.py` (**new**) | `seed_bulk_e2e.py`, `seed_test_catalog.py` | — |

Still unguarded, reported rather than changed:

- **`alembic upgrade head`** — no fence anywhere. `start_backend.sh` runs it on every start; `main.py` does not, so on your run command a migration only lands when someone runs alembic deliberately.
- **Playwright specs that talk to a real backend over HTTP** rather than through `BackendDatabase` — the helper fence cannot see them.
- **Any future script** reading `settings.DATABASE_URL` without calling `require_disposable_database`.
- **`reconcile_branch.py`** — same unguarded pattern as the seeds, and it *writes*. Not named in the task; **recommend fencing it next.**

### 8.4 Risks in what did ship

- **`repairCrossOrgCustomers` deletes rows**, at login, on a production device DB. Scoped to `organization_id <>`, never the current org, keeping any row with an unsent event; idempotent and non-fatal — but it is the highest-blast-radius change here. It deletes Kwame's **device** row (which is the point); his server row is untouched and he stays skipped by the backfill.
- **`repullAggregateOnce` is ~45 lines**, above the ~30 the task suggested. I judged the line count the wrong thing to optimise for, given it completes the Kwame convergence story, and flagged it rather than shipping it quietly.
- **Server-side `sync_status` for customers is now `synced` at emit time.** If anything else treated `customers.sync_status == 'pending'` as a "needs pushing" signal, that meaning has changed.
- **The offline award emits from inside a projector**, bending ADR 0007 the same way `prescription_refill_used` already does. Idempotency rests on the `RETURNING id` gate plus the derived id.
- **Old-build devices silently miss loyalty convergence** (§8.1b).

### 8.5 Open questions

1. **The UI observation is consistent with the two-branch page, but I have not reproduced it live.** The backend is up on `127.0.0.1:8000` and the app resolves that same port, so the offline/online split has an obvious reading: offline → device SQLite → the two local rows (Joe 0, Mike 0); online → `customersApi.list()` → the server's three (Joe 125, Mike 0, Kwame 150). That matches the addendum's conclusion. It remains an inference about your session: with 49 foreign rows plus a Kwame row under `11111111-…`, the device's own customer list was *wider* than 2 before the cleanup — and `searchCustomers` was unscoped-safe only by accident of which branch ran. Worth a look after step 5.
2. **Was the 49-row foreign block ever a real leak?** `searchCustomers` without an org returned every org, and the device held 49 foreign customers. Any page or report reading customers unscoped would have shown them. I fixed the read path but cannot tell from here whether anyone saw them.
3. **Should the backfill publish Kwame's balance?** He is skipped. If the cleanup deletes his device row, his Customers list will lack him until a fresh sync re-delivers seq 25 — which step 6 can force. Whether his *server* row should also be retired is yours and is untouched.
4. **What is `total_orders` supposed to mean** — orders ever rung up, or orders kept? The refund behaviour is only wrong under the second reading, and the answer decides option 1 in §8.2.
5. **Old-build rollout policy.** Should the server hold `customer_loyalty_changed` for clients that predate this build, or is "update the device" acceptable? See §8.1b.
6. **Should `purchase_order` join `AGGREGATE_TABLES`?** Verified safe, deliberately out of scope.
7. **Who owns the parity fixtures?** A backend integration test writing a tracked UI fixture will keep dirtying the repo until that is decided.
