# Customers sync and loyalty fix — implementation report

**Branch:** `fix/customers-sync` (from `feat/rx-sync-and-forms` @ `88162ed`)
**Date:** 2026-10-06 · **Not pushed.** No remote was touched.
**Worktree:** `/tmp/pharmacare-fix/wt` — the main tree was never modified; it still
sits on `feat/rx-sync-and-forms` @ `88162ed` with its four untracked docs and both
stashes intact.

Nine commits. All work is in the worktree; the disposable Postgres cluster was
destroyed after the run.

---

## 1. Findings that changed the design

Five things turned out differently from what the investigation report implied, and
each one changed what I built.

**1.1 The misattribution was never live — it was latent.**
The investigation's headline was a permanent cross-tenant misattribution. Every
one of Kwame's 10 events has envelope `org_id == payload.organization_id`; the
mismatch in the original report was synthetic, constructed in a harness. Worse for
the "actively harmful" framing: even a *self-consistent* event naming a
non-existent org passes `CustomerProjector` validation, because `_validate_created`
only compares the payload against the envelope. Only the
`customers_organization_id` FK stops it. So the real defect is a missing
defence-in-depth check, and I hardened both sides rather than "fixing" a breach.

**1.2 The device never had a snapshot writer at all.**
I expected to find a bootstrap/sync path that copies server customers into the
device. There is none. The only writers of the device `customers` table are the
three event projectors plus `writeLocal.customer`. That changes the Phase 3 answer:
no amount of re-pulling can repair a device customer row, because a pull carries
only events, and the correct-org event for Kwame is at seq 25, far behind the
cursor. Hence the scoped re-pull.

**1.3 The device table cannot hold the order aggregates.**
`localDb.ts:431` never created `total_orders` / `total_value`, `migrate_v27` adds
six other columns but not these, and no projector writes them. So Phase 3.3's
"update total_orders/total_value exactly as online does" is **server-side only** —
I did not add the columns, per instruction. The device's Joe row is 0 points and
has no order count at all.

**1.4 `_customerUpdated` is a genuine data-loss path, and it is now unreachable
for loyalty.**
It overwrites every field present in the payload with no `sync_status` guard, so a
server event would silently discard offline edits on a pending row. Because the
new event carries a balance and nothing else, routing it through that handler would
have walked straight into this. It gets its own handler that touches two columns.
This is also why the event is a new type rather than a `customer_updated`: the
investigation flagged the overwrite risk, and the design confirms it.

**1.5 The header overlap does not exist.**
The task said to fix the orange "offline" pill overlapping the primary action
button "if it still overlaps". At `88162ed` there is no orange pill in either
header: `CustomersPage` and `PrescriptionsPage` both use a plain
`flex items-center justify-between` row with no absolute positioning, and the
`SyncIndicator` (the only offline pill) lives in the AppShell sidebar, not a page
header. I made no change and invented no substitute. One latent risk noted below.

---

## 2. Commits and files

| Commit | What |
|--------|------|
| `41a72a8` | `searchCustomers` / `searchCustomerMatches` fail closed without an org; `_customerCreated` rejects cross-org and same-id-different-org events |
| `3d8a7c1` | migration v37 foreign-org customer cleanup; `AGGREGATE_TABLES` extended to customer/sale/drug_batch/branch_inventory; `repairStalePendingMarkers` |
| `724f230` | optional scoped one-shot re-pull (`repullAggregateOnce`) |
| `502629d` | `customer_loyalty_changed` event contract, shared id derivation, ADR 0010, server emits on online sale + refund, `SaleProjector` awards offline |
| `4a8a282` | device loyalty projector; `customers:changed`; `CustomersPage` + POS typeahead subscribe |
| `24b5d26` | `backfill_customer_loyalty_events.py` |
| `3a1635d` | fence the two E2E seed scripts against a real database |
| `9cfc35a` | S-A..S-H regression suite |
| `db19dbd` | fence import works as both a package member and a direct script |

**Files**

*Device (`ui.laso/src/`)*
- `lib/localRead.ts` — `searchCustomers` requires the org; `searchCustomerMatches` too
- `lib/localProjectors.ts` — `_customerCreated` org validation; new `_customerLoyaltyChanged`
- `lib/localDb.ts` — v37 + `repairCrossOrgCustomers`; `AGGREGATE_TABLES`; `repairStalePendingMarkers`
- `lib/syncEngine.ts` — `repullAggregateOnce`
- `lib/loyaltyEventId.ts` — device half of the id derivation (new)
- `lib/__tests__/customersSyncPhase1.test.ts`, `foreignCustomerCleanup.test.ts`, `stalePendingMarker.test.ts`, `scopedRepull.test.ts`, `customerLoyaltyProjector.test.ts`, `customersSyncScenarios.test.ts`, `loyaltyEventId.spec.ts` (new)
- `pages/CustomersPage.tsx`, `pages/PrescriptionsPage.tsx`, `components/pos/CartPanel.tsx`
- `lib/__tests__/localDbPrescriptionLicenseOptional.test.ts` — asserts the chain head, not 36

*Server (`backend.laso/`)*
- `app/services/sales/utils/loyalty.py` — the one award helper (new)
- `app/services/sync/eventlog/loyalty_event_id.py` — id derivation (new)
- `app/services/sync/eventlog/projectors/sale.py` — `_apply_offline_loyalty`
- `app/services/sync/eventlog/projectors/customer.py` — `customer_loyalty_changed` validate + apply
- `app/services/sales/sales_service.py` — emit on online sale and on refund
- `scripts/backfill_customer_loyalty_events.py`, `scripts/refuse_real_database.py` (new)
- `scripts/seed_bulk_e2e.py`, `scripts/seed_test_catalog.py` — fenced
- `tests/integration/test_loyalty_convergence.py`, `tests/unit/test_loyalty_event_id_vectors.py`, `tests/unit/test_refuse_real_database.py` (new)
- `docs/decisions/0010-customer-loyalty-event-contract.md` (new)

---

## 3. Event contract and decision record

Full text: `docs/decisions/0010-customer-loyalty-event-contract.md`.

**Event:** `customer_loyalty_changed`, `aggregate_type=customer`,
`aggregate_id = customer_id`.

```json
{
  "customer_id": "<uuid>",
  "organization_id": "<uuid>",
  "loyalty_points": 125,
  "loyalty_tier": "silver",
  "sale_id": "<sale id or number>",
  "direction": "earn" | "refund",
  "source": "online_sale" | "offline_sale" | "refund" | "backfill"
}
```

**Id rule:**

```
sha256("customer_loyalty_changed|<customer_id>|<sale_id>|<direction>")
  -> first 26 characters, uppercased
```

via the existing `deterministic_event_id`, the same helper the refill id and the
stock backfills use. Mirrored on the device in `lib/loyaltyEventId.ts`; both sides
are pinned to the same four vectors by `test_loyalty_event_id_vectors.py` and
`loyaltyEventId.spec.ts`, so a change to either formula fails both suites.

**Why `direction` is in the seed.** If a sale's earn and that sale's refund shared
an id, `event_log`'s `(org_id, event_id)` primary key would swallow the reversal as
a duplicate of the earn, and the customer would keep the points they had just given
back. The helper raises `ValueError` on an unknown direction rather than derive an
id that could collide.

**Ordering.** Emitted inside the same transaction, after the points are written,
through `StockEventEmitter.emit_in_transaction`, which does not swallow errors — so
a failed append rolls the points back. If the points commit, the event is in the
log.

**Replay behaviour.**

| Situation | Result |
|---|---|
| Same event twice | Absolute value, so an assignment. No drift. |
| `sale_created` re-projected | `RETURNING id` returns None → the award block is never reached. |
| Earn then refund | Two distinct ids; the balance returns to where it started. |
| Refund before its earn | Both absolute; last applied wins on a real value. No counter to go negative. |
| Event for an unknown customer (device) | Recorded via `recordEventProjectionFailure`; the row is never created. |
| Tier contradicting the points | Rejected `loyalty_tier_inconsistent` on the server; quarantined on the device. |

**Duplicates are safe for three independent reasons**, so no single one has to hold:
the payload is absolute; the id is derived from the change's identity so the append
path refuses the duplicate; and the server is the only writer of the balance.

**Sync_status.** `process_sale` now calls `customer.mark_as_synced()` after a
successful append. Previously the row went to `pending` with no event behind it,
which is exactly Joe's state. There is no server-side reconciler for its own
emitted events, so marking it at the emit site is the consistent choice and mirrors
the device-side reconciliation added in `88162ed`.

---

## 4. Tests before / after

| Suite | Before | After | Delta |
|-------|--------|-------|-------|
| UI files | 73 passed | **80 passed** | +7 |
| UI tests | 530 passed | **587 passed** | +57 |
| `tsc --noEmit` | clean | **clean** | — |
| Backend unit | 216 passed | **240 passed** | +24 |
| Backend integration | 184 passed | **203 passed** | +19 |
| **Backend total** | 400 | **443** | **+43** |

New tests, all green:

| File | Tests | Covers |
|------|-------|--------|
| `customersSyncPhase1.test.ts` | 7 | fail-closed reads; cross-org create; same-id collision |
| `foreignCustomerCleanup.test.ts` | 6 | 49 foreign rows; the Kwame chain; unsent-event rows kept; idempotent |
| `stalePendingMarker.test.ts` | 9 | Mike's state; all four new aggregates; conflict never swept |
| `scopedRepull.test.ts` | 5 | scoped pull leaves the cursor byte-identical |
| `loyaltyEventId.spec.ts` | 8 | cross-runtime vectors |
| `customerLoyaltyProjector.test.ts` | 9 | writes two columns only; idempotent; unknown customer; cross-org |
| `customersSyncScenarios.test.ts` | 13 | S-A..S-H |
| `test_loyalty_convergence.py` | 19 | online, offline, replay, refund, walk-in, drift, invariant |
| `test_loyalty_event_id_vectors.py` | 9 | cross-runtime vectors |
| `test_refuse_real_database.py` | 15 | the script fence |

Two existing tests needed correction rather than my code:

- `localDbPrescriptionLicenseOptional.test.ts` asserted the migration chain stopped
  at exactly 36. It now asserts the chain reached its head (37), which is what the
  test was for.
- The fence import broke `test_catalog_seed_service.py`'s collection; fixed in
  `db19dbd`.

**One pre-existing test I did not fix**, because it is a cross-tree contract change
rather than a small gap: `backend.laso/tests/integration/test_stock_event_device_parity.py:57`
**writes** the tracked UI fixture
`ui.laso/src/lib/__tests__/fixtures/stock-parity.json`. Every backend integration
run therefore leaves the repo dirty and races the UI suite on that file. I
reproduced it twice and reverted the file each time. Fixing it means deciding where
the fixture lives and who owns it.

---

## 5. Backfill dry-run table

Run against **atlasdb**, `READ ONLY` session, nothing written. `--apply` was **not**
run against atlasdb at any point; it was exercised only against a disposable
cluster.

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

Verified on a disposable cluster: first `--apply` appended 2; the second appended 0
and reported `already present: 2`. `--apply` without `--i-have-a-backup` refuses.

Kwame Nkrumah is **not deleted, not modified**, and is printed as
`skipped - owner decision pending`. The script additionally refuses to publish any
row whose stored tier contradicts `resolve_loyalty_tier(points)`.

---

## 6. Owner steps

**Do not run these blind — step 0 first.**

### Step 0 — what is actually running

The PharmaCare backend **is** running, as `python main.py`. Verified:

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

`main.py:241-255` runs uvicorn on `127.0.0.1:8000` with `reload = ENVIRONMENT !=
"production"`, and `.env` sets `ENVIRONMENT=development`, so **the reloader is
active** (pid 175371 is uvicorn's spawned worker). It currently serves commit
`88162ed` from the main tree.

**The port is 8000, and that is what the app uses — but read `.env.local`, not
`.env`:**

| File | `VITE_API_URL` | Effective? |
|------|----------------|-----------|
| `ui.laso/.env` | `http://127.0.0.1:8001` | no — overridden |
| `ui.laso/.env.local` | `http://127.0.0.1:8000` | **yes** |

Vite resolves `.env.local` over `.env`, so the app talks to
`http://127.0.0.1:8000`, which is exactly where `python main.py` listens. **The two
agree and there is no port mismatch.** Port 8001 is simply unused.

One unrelated thing on this host, so nobody "fixes" it by mistake: a Docker
container `kratos_backend` (image `election-system-backend`) also runs uvicorn on
`0.0.0.0:8000`, inside its own network namespace. It is a different application
and is **not** what serves `127.0.0.1:8000` — that is pid 174977. Leave it alone.

Also running: the Tauri dev app from the main tree (`tauri.js dev`, Vite on 1420,
`target/debug/pharmacare`), so the UI is currently serving `88162ed` too.

**No database migration is part of this change.** The only schema change is the
device-side SQLite migration v37, which the app applies itself at startup. There is
no new Alembic revision, so `alembic upgrade head` is not required — but running it
is harmless.

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

Expect non-zero counts, and both `customers` and `event_log` present. A zero count
means the dump is empty — stop.

*Rollback:* none needed; this only reads. If the file is bad, delete it and redo.

### Step 2 — backfill dry-run

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
set -a; . ./.env; set +a
export DATABASE_URL="${DATABASE_URL/postgresql+psycopg2/postgresql+asyncpg}"
export SECRET_KEY="dry-run-only" ENVIRONMENT=test
/home/vermithor/lasoenv/bin/python scripts/backfill_customer_loyalty_events.py
```

Compare against §5. It opens `READ ONLY`, so a write would be refused by
PostgreSQL itself.

*Rollback:* nothing was written.

### Step 3 — backfill apply (only after a verified dump)

```bash
/home/vermithor/lasoenv/bin/python scripts/backfill_customer_loyalty_events.py --apply --i-have-a-backup
```

Expect `appended 2  already present 0  failed 0`. Re-running must report
`to emit: 0`.

*Rollback:* the events are additive. Restoring the dump removes them:
```bash
pg_restore --clean --if-exists --dbname="$DATABASE_URL" "$OUT"
```
That is destructive to everything written since the dump — restore only if you have
nothing else to lose, and prefer leaving the events in place, which is harmless:
the device handler writes two columns.

### Step 4 — switch the main tree (the backend reloads itself)

```bash
cd /home/vermithor/Desktop/inventory/com.pharma
git status                     # expect: only the 4 untracked docs
git fetch origin               # read-only
git checkout fix/customers-sync
git log --oneline -1           # expect 2a0f7c5
```

**No backend restart is needed.** `python main.py` runs with `reload=True`, and
uvicorn watches `backend.laso`, so the checkout triggers a reload on its own. Watch
for it in the terminal running `python main.py`, then confirm:

```bash
sleep 3
curl -s -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:8000/health
```

If `/health` does not answer, the reloader did not pick it up — restart it yourself:

```bash
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso
python main.py
```

Restarting the UI is recommended rather than assumed: the checkout rewrites many
files at once and Vite's HMR can be left in a confused state.

```bash
pkill -f 'tauri.js dev'
cd /home/vermithor/Desktop/inventory/com.pharma/ui.laso && pnpm tauri dev
```

*Rollback:* `git checkout feat/rx-sync-and-forms`. The reloader picks that up too;
restart `python main.py` only if it does not.

**Which commit is the running app using?**

```bash
cd /home/vermithor/Desktop/inventory/com.pharma && git rev-parse --short HEAD
```

### Step 5 — log in once so the repairs run

The v37 cleanup and the stale-pending repair both run inside `login`. Log out and
back in on the device, then read the console:

```
[auth] cross-org customer cleanup: removed 49 row(s)
[auth] stale pending-marker repair: N row(s) (customers=N, sales=N, ...)
```

*Rollback:* the repairs are one-shot and idempotent. Foreign rows return with the
next sync if that org is ever signed in here again.

### Step 6 — optional: force the customer re-pull

After the cleanup removes the foreign Kwame row, the real-org Kwame event (seq 25)
will **not** re-arrive on its own — the cursor is past it. Optional one-shot, does
not touch the cursor:

```js
// DevTools console, while signed in
const { repullAggregateOnce } = await import("/src/lib/syncEngine.ts");
await repullAggregateOnce("customer", "<your org id>");
```

Expect `applied 1 envelope(s); stored cursor untouched`.

*Rollback:* it only applies events the server already holds.

### Step 7 — verify

DevTools, on `laso.db` (read-only queries):

```sql
-- customers per org
SELECT organization_id, COUNT(*) FROM customers GROUP BY organization_id;

-- Joe and Mike
SELECT substr(id,1,8) id, loyalty_points, loyalty_tier, sync_status
  FROM customers WHERE substr(id,1,8) IN ('5823ef27','0615fb0e');

-- outbox backlog
SELECT status, COUNT(*) FROM event_outbox GROUP BY status;

-- pull cursor
SELECT key, value FROM sync_meta WHERE key LIKE 'event_pull_seq%';
```

Expected: only your org under the first query; Joe `125/silver`, Mike
`0/bronze`, no `pending` rows left stranded; `event_pull_seq` unchanged.

---

## 7. Risks and things not fixed

**Not fixed — deliberate, per scope**

1. **Refund still does not reverse `total_orders` / `total_value`.** Reproduced and
   pinned by a test (`test_refund_does_not_reverse_order_counters`): points go
   30 → 0 while orders stay 1 and value stays 30.0. After a refunded sale the
   customer keeps claiming spend that was undone. Options, for a decision:
   - *Full reversal on full refund, none on partial* — matches the money, but a
     partial refund then needs proportional order counting, which is ambiguous.
   - *Proportional on partial* — `round(refund_amount / sale_total)`; fractional
     order counts need a convention (floor? carry?).
   - *Leave it, and treat the counters as "orders ever rung up" rather than
     "orders kept"* — cheapest, but then rename the fields so the meaning is honest.
   I did not change it: it rewrites historical aggregates nobody has audited.
2. **No points ledger.** `loyalty_points` is still a counter. A lost award is still
   unrecoverable in principle — though with the event published on every change,
   the balance is now reconstructible from the log. Deferred by owner decision.
3. **No redemption implementation.** Thresholds exist, ledger does not.
4. **The other `localRead` truthiness guards.** `searchDrugs`,
   `searchContracts` and the function at `localRead.ts:1358` all guard their org
   filter on truthiness. Same class of bug, same exposure, not fixed — out of scope.
   `searchCustomerMatches` was fixed because it is a customer read.
5. **`stock` aggregate cannot be reconciled.** There is no local
   `stock_adjustments` table at all (documented in `localWrite.ts:888-902`), so there
   is no row to flip. `purchase_order` *could* be added safely and was left out only
   as out of scope.
6. **Header overlap.** Not reproducible; no change made. Latent residual: neither
   page header's right-hand button group has `flex-wrap`, so a very narrow window
   could still crowd the title. Not touched because the reported symptom does not
   exist and I will not invent a fix.

**What can still write to a real database.** Verified coverage:

| Guard | Covers | Does NOT cover |
|---|---|---|
| `tests/conftest.py` (`e8dd90c`) | any pytest run whose `TEST_DATABASE_URL` names atlasdb/postgres/template0/template1 | scripts, Playwright, alembic |
| `tests/e2e/helpers/backend-db.ts` (`213b6fc`) | the Playwright `BackendDatabase` helper | anything not using that helper |
| `scripts/refuse_real_database.py` (**new**) | `seed_bulk_e2e.py`, `seed_test_catalog.py` | — |

Still unguarded, and reported rather than changed:

- **`alembic upgrade head`** — no fence anywhere. `start_backend.sh` runs it on
  every start; `main.py` does not, so on your run command a migration is only
  applied when someone runs alembic deliberately.
- **Playwright specs that talk to a real backend over HTTP** rather than through
  `BackendDatabase` — the helper fence cannot see them.
- **Any future script** that reads `settings.DATABASE_URL` without calling
  `require_disposable_database`.
- **`reconcile_branch.py`** — same unguarded pattern as the seeds, and it *writes*.
  I fenced the two the task named; this one was not named and I did not want to
  change its behaviour unasked. **Recommend fencing it next.**

**Risks in what I did ship**

- **`repairCrossOrgCustomers` deletes rows.** Scoped to `organization_id <>`, never
  the current org, and it keeps any row with an unsent event. Still a delete
  against a production device DB at login — it is idempotent and non-fatal, but it
  is the highest-blast-radius change here. Kwame's row is deleted **by the
  cleanup**, which is what the owner asked for; his server row is untouched and he
  remains skipped by the backfill.
- **`repullAggregateOnce` is ~45 lines**, above the ~30 the task suggested. I judged
  the line count the wrong thing to optimise for given it completes the Kwame
  convergence story, and flagged it rather than quietly shipping it.
- **Server-side sync_status for customers** is now marked `synced` at emit time. If
  anything else depended on `customers.sync_status == 'pending'` as a "needs
  pushing" signal, that meaning has changed.
- The offline award runs inside `SaleProjector.apply`, bending ADR 0007 the same way
  `prescription_refill_used` already does. Idempotency rests on the `RETURNING id`
  gate plus the derived id.

---

## 8. Open questions

1. **The UI observation is consistent with the two-branch page, but I have not
   reproduced it live.** The backend is up on `127.0.0.1:8000` and the app resolves
   that same port, so the offline/online split has an obvious reading: offline, the
   page reads the device's SQLite and shows the two local rows (Joe 0, Mike 0);
   online, it takes `customersApi.list()` and shows the server's three (Joe 125,
   Mike 0, Kwame 150). That matches the addendum's conclusion. It is still an
   inference about *your* session, though: I did not drive the app, and the 49-row
   foreign block plus a Kwame row under `11111111-…` mean the device's own customer
   list was wider than 2 before the cleanup. Worth a look after step 5.
2. **Was the 49-row foreign block ever a real leak?** `searchCustomers` without an
   org returned every org, and the device held 49 foreign customers. Any page or
   report that read customers unscoped would have shown them. I fixed the read path
   but cannot tell from here whether anyone saw them.
3. **Should the backfill publish Kwame's balance?** He is skipped. If his device
   row is deleted by the cleanup, his Customers list will simply lack him until a
   fresh sync re-delivers his `customer_created` (seq 25) — which the optional
   re-pull can force. Whether his *server* row should also be retired is the
   owner's call and is untouched.
4. **What is `total_orders` supposed to mean** — orders ever rung up, or orders
   kept? The refund behaviour is only wrong under the second reading. The answer
   decides option 1 in §7.
5. **Should `purchase_order` join `AGGREGATE_TABLES`?** Verified safe, deliberately
   out of scope.
6. **Who owns the parity fixtures?** A backend integration test writing a tracked UI
   fixture will keep dirtying the repo until that is decided.
7. **Anything else in the sync surface?** Not investigated — out of scope, listed
   only so it is not assumed covered.
