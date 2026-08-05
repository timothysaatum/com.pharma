# Independent Engineering Review — Inventory, Data Sync & Sales/POS

**Date:** 2026-08-04
**Scope:** Backend (`backend.laso`) + Frontend/Tauri (`ui.laso`) + offline sync layer (`crsqlite`)
**Method:** Read of current code (models, services, endpoints, sync engine) plus the full prior investigation record (`docs/decisions/`, `docs/evidence/`, `docs/investigations/`, `docs/plans/`), cross-checked against recent commit diffs. This review does not repeat findings already fixed and verified in that prior record — it cites them and builds forward.

---

## 1. Executive summary

**Verdict: not ready to onboard a second tenant onto shared production infrastructure.** The transactional core of the system — inventory locking, FEFO allocation, sale processing, refunds, idempotency — is unusually rigorous and should not be re-architected. The problems are concentrated in two places: **the sync layer has no multi-tenant isolation**, and **branch/organization authorization is implemented ad hoc per-endpoint**, so the same bug class (fixed once, in `docs/decisions/0005`) has recurred at least three more times in three different directions (too loose, too strict, too loose-when-empty).

Two findings are P0 blockers on their own:

1. **Every organization's client can pull and merge every other organization's customers, prescriptions, drug batches, and purchase orders into its local database** — the CR-SQLite sync layer has no org/branch scoping (§4.2, Finding S1).
2. **Any cashier can permanently and silently erase a completed, money-and-drugs-already-exchanged sale from the system of record**, with no approval gate and no server-side audit trail (§4.3, Finding P3).

Neither requires a redesign to fix, but both must be fixed — and re-verified with regression tests — before this system carries more than one organization's data, or before it's trusted as the system of record for controlled-substance dispensing.

---

## 2. Cross-cutting pattern: authorization is reinvented per endpoint

`docs/decisions/0005-purchase-order-branch-authorization-gap.md` already diagnosed and fixed one instance of this: a non-elevated user with no assigned branches must see **nothing**, not everything. That fix was applied to one endpoint (`GET /purchase-orders/`) and never generalized. This review found the same bug class recurring in three more shapes:

| # | Location | Failure mode | Direction |
|---|---|---|---|
| A | `purchase_order_endpoints.py` single-object routes (`get`, `submit`, `approve`, `reject`, `cancel`, `receive_goods`) | Only checks `organization_id`, never `assigned_branches` | **Too loose** — cross-branch access within an org |
| B | `prescription_endpoints.py::list_prescriptions` (line 461) and `search_customer_prescriptions` (line 629) | `if not is_super_admin and assigned_strs:` — when `assigned_strs` is empty, the filter is skipped entirely | **Too loose** — org-wide PHI leak to a user with zero branch assignments |
| C | `inventory_endpoints.py` (8 call sites) | Checks membership only, no `is_super_admin` bypass that every sibling router has | **Too strict** — admins locked out of inventory for branches they're not explicitly assigned to |

The root cause is structural, not a series of unrelated typos: **every router hand-rolls its own branch-filter conditional**, so there is no single place a fix propagates from. `sales_endpoints.py::list_sales` (lines 142–155) is the one router that does this correctly (explicit `filters.append(false())` when the accessible-branch set is empty) — it should be the template.

**Recommendation:** extract one shared authorization primitive — a FastAPI dependency or query-builder mixin, e.g. `scope_to_accessible_branches(model, current_user)` — that every list/get/mutate endpoint calls, with a single well-tested implementation of the three rules (super admin → no filter; has assigned branches → filter to them; has none → return nothing). Then add one parametrized test that runs the "non-elevated user, zero assigned branches" scenario against **every** router automatically, so this class of bug fails CI instead of being rediscovered domain-by-domain.

---

## 3. Domain findings

### 3.1 Inventory

**Architecture.** Stock is modeled across `drugs` → `branch_inventory` (cached total + `reserved_quantity` + optimistic `version_id`) → `drug_batches` (FEFO/expiry, lot traceability) → `stock_adjustments` + `InventoryMovement` (audit ledger). All mutating paths go through `InventoryService` using `SELECT … FOR UPDATE` plus `begin_nested()` savepoints. `BranchInventory.quantity` is deliberately kept as a derived cache, re-synced from `SUM(drug_batches.remaining_quantity)` after every batch write — a sound anti-drift design. Transfers between branches are atomic (source + destination in one savepoint). This is solid, defensible engineering; do not re-architect it.

**Gaps found:**

- **I1 — Duplicate batch receiving is hard-rejected, contradicting the documented spec.** `create_batch` and `receive_goods` both raise HTTP 400 on a repeat `(branch_id, drug_id, batch_number)`, but `docs/plans/pharmacy_audit_plan.md` (§5.6) documents the intended behavior as *append to the existing batch*. A supplier delivering the same lot across two shipments — routine — currently forces staff to fabricate a distinct batch number, which corrupts the lot traceability the system exists to preserve. *(`inventory_service.py:1168-1183`, `purchase_order_service.py:640-655`)*
- **I2 — Inventory endpoints missing the `is_super_admin` bypass.** 8 call sites in `inventory_endpoints.py` check only `assigned_branches` membership; every sibling router (`drug_endpoints.py`, `sales_endpoints.py`, `prescription_endpoints.py`) ORs in `is_super_admin`. An org owner not explicitly assigned to a branch gets 403 on all inventory reads/writes for it while retaining full access to that branch's sales and prescriptions. *(too-strict counterpart to §2)*
- **I3 — Purchase-order single-object routes ignore branch entirely** — see §2, row A.
- **I4 — `export_inventory_excel` has no permission or branch check at all.** Already flagged in `docs/investigations/security_audit.md` (remediation item, line 82); confirmed still unresolved. Any authenticated user can export the whole org's inventory. *(`export_endpoints.py:51-79`)*
- **I5 — No test exercises real concurrent DB connections.** The one test claiming to cover concurrent stock locking (`test_concurrent_sales_do_not_produce_negative_stock`) runs two calls sequentially on one shared session; its own docstring admits true parallel concurrency isn't tested anywhere. The `FOR UPDATE` locking design is sound in principle but unverified under genuine concurrent connections (two POS terminals selling the last unit; two simultaneous `receive_goods` on the same PO).
- **I6 — No unique DB constraint on `(branch_id, drug_id, batch_number)`** (intentionally, for CRR conflict resolution), combined with I1's plain pre-check, means two simultaneous genuine-new-batch receipts can still race past the duplicate check.

**Already solid — do not re-flag:** row locking + savepoints across every write path; `CheckConstraint`s preventing negative stock at the DB layer; consistent FEFO server-side and offline; automatic quantity-drift correction; a genuinely comprehensive audit ledger; the PO **list**-endpoint fix from decision 0005 (well tested, 7 scenarios); CR-SQLite merge correctness for inventory tables has real-Postgres E2E evidence.

### 3.2 Data synchronization (CR-SQLite / CRDT)

**Architecture.** The client runs local SQLite + the CR-SQLite extension; 10 tables are CRR-tracked. The backend runs its own SQLite "shadow DB" as a genuine CR-SQLite peer (per `docs/decisions/0003`), not a passive relay: client pushes raw `crsql_changes` → shadow DB merges via CR-SQLite triggers → validated upsert into Postgres. Pull is the mirror operation. Per-table merge strategy is now generalized across 4 strategies (`lww`, `sum_and_merge`, `keep_both_renumber`, `lww_with_external_dedup`), a real fix over the earlier hardcoded-to-one-table state. `sales` is deliberately excluded from generic CRDT merge and kept on a separate validated push path (`docs/offline-sales.md`) because sale creation has side effects (FEFO, ledger, prescriptions) a blind row-merge would bypass — this is correct design, not an oversight. The investigation history (rusqlite swap → CR-SQLite spike → phased CRR migration) is competent work: each prior doc closes a real blocker with evidence against real Postgres, not hand-waving.

**Gaps found:**

- **S1 — No organization/branch scoping on CRR pull. This is the most serious finding in the whole review.** `ShadowDB.get_changes_since()` runs `SELECT … FROM crsql_changes WHERE db_version > ? ORDER BY …` with no org/branch filter — `crsql_changes` has no such column and nothing joins one in. The client filters only its own `site_id`. **Every pharmacy organization's desktop client currently pulls and locally merges every other organization's customers, prescriptions, drug batches, and purchase orders.** No test anywhere exercises cross-org pull scoping. *(`shadow_db.py:983-1010`, `crr_sync_endpoints.py:112-186`, `syncEngine.ts:768`)*
- **S2 — Missing per-table push validators make cross-tenant writes possible, not just reads.** Only `branch_inventory` has a push validator, and even it doesn't check `branch_id` ownership. `upsert_merged_row` writes whatever `organization_id`/`branch_id` the client embedded straight into Postgres via `ON CONFLICT … DO UPDATE SET <every column>`. Combined with S1, a client can overwrite another org's row by referencing its `id`. *(`crr_sync_service.py:42-84`, `shadow_db.py:1096-1178`)*
- **S3 — Dead-lettering for `sales` is silently defeated: genuine infinite retry with no admin-visible failure state.** `reconcileOfflineSales()` (commit `3974a03`) unconditionally resets `attempts=0` for any sale at the dead-letter threshold, *before* the queue-state read that would surface it as blocked in the UI. A permanently un-syncable sale (bad FK, corrupted payload) now retries forever, silently, every cycle. *(`syncEngine.ts:686-722`)*
- **S4 — Transient-vs-permanent sync error classification is a fragile, partially-broken string match across languages.** `isDeferredRx` checks `error.includes("Prescription not yet synced")`, a string the backend never actually emits (checked `sync_service.py`) — only the second substring in the OR does real work. This untyped, unversioned string contract between Python and TypeScript can silently break either direction on any backend wording change. *(`syncEngine.ts:445`, commit `162418e`)*
- **S5 — Tombstone gaps.** `branch_inventory` and `drug_batches` have no `is_deleted` column at all; `prescriptions`/`purchase_orders` rely on a `status` field as an unconfirmed soft-delete proxy. Matches `sync-audit-gaps.md` gap #3, now scoped narrower.

**Already solid — do not re-flag:** field-level CRDT merge, HLC conflict resolution, BLOB round-trip, and all four merge strategies have real-Postgres E2E evidence across three dated evidence docs; the cursor-coupling and paginated-pull-stall bugs were found and fixed with a bounded retry guard (throws after 3 stalled attempts) — a good fix, unlike S3; `sales` being excluded from generic CRR merge is architecturally correct; the v22 schema migration handles pre-existing broken local schemas transactionally with rollback.

**Independent assessment:** CR-SQLite plus a server-side shadow-DB peer is the right architecture for this problem — it solves field-level merge and false conflicts essentially for free, and the choice is backed by real evidence, not a hunch. The risk is not the foundation; it's that "the server is just another CRDT peer" was implemented without the multi-tenancy dimension CR-SQLite has no native concept of. That is fixable without a rewrite (see §5.1). S3/S4 are process risk from fixing "stuck pending" symptoms without a systematic transient-vs-permanent failure taxonomy — they would recur under any sync architecture that lacks one, so fixing the taxonomy is more valuable than re-patching each symptom.

### 3.3 Sales / POS

**Architecture.** Online checkout (`SalesService.process_sale`) is a single savepoint doing, in order: idempotency check via `client_sale_id`, allergy check (persisted independently of savepoint rollback — a genuinely safety-conscious design), FEFO batch pre-load with row locks, combined stock+prescription validation collecting *all* errors at once, signed-token contract validation for insurance pricing, price resolution (`selling_price` chain, never `cost_price`), reservation, per-item pricing with exclusions/caps/floors, persistence, FEFO deduction with per-batch allocation rows and a ledger entry, refill decrement, loyalty points, commit, then a separately-committed audit log so a failed audit write can't roll back a completed sale. Offline checkout re-validates stock and prescription status against the local mirror, computes pricing client-side, and writes one atomic local transaction keyed by the same UUID used as idempotency key end-to-end. Sync-back re-locks and re-validates stock server-side (tested: `test_multiple_offline_sales_oversell_blocked`), evaluates expiry as-of the offline creation time, and re-checks `requires_prescription` against live catalog data independent of client claims. Refunds require manager approval and track remaining refundable quantity per batch allocation. This is materially more rigorous than a typical POS system.

**Gaps found:**

- **P1 — Offline sale prices are never reconciled against server truth.** `_push_sale` only checks a submitted sale's numbers are internally self-consistent; it never compares the offline `unit_price` against the branch's *current* `selling_price` or re-derives the contract discount. If prices change while a terminal is offline — a routine event, not an edge case — every offline sale permanently locks in the stale price with no flag, no report, and no test coverage of this scenario. This is silent, recurring revenue leakage or overcharging.
- **P2 — Same authorization-gap class as §2, row B, in the exact endpoint the POS prescription lookup uses at checkout** (`search_customer_prescriptions`) — a non-elevated user with zero assigned branches gets org-wide prescription data, including controlled-substance history.
- **P3 — The sync-failure "Discard" action can erase a completed sale with no approval, no confirmation, and no server-side audit.** `SyncIndicator.tsx` renders a one-click "Discard" for any dead-lettered sale, visible to any authenticated user. `discardFailure` just flips local `sync_status` to `'synced'` and dequeues it — contrast with `refund_sale`/`cancel_sale`, both of which require a manager-approval user. A sale that fails to sync because the server correctly rejects it (e.g., a stock conflict on an already-dispensed transaction) can be permanently wiped from the record of truth by the cashier who sees the badge. **This is the single highest-impact finding in the Sales domain** — worse than a pricing error, because it actively destroys the audit trail rather than just misstating a number.
- **P4 — Offline pricing/discount math is a hand-written duplicate of the server's `pricing_calculator.py`**, missing contract exclusions, per-drug overrides, and discount caps/floors that the server enforces online. Combined with P1, this divergence is never caught.
- **P5 — No test exercises price-drift-during-offline**, despite stock/expiry/backdating/idempotency all being well covered.

**Already solid — do not re-flag:** overselling across concurrent offline terminals is well-handled and well-tested; prescription enforcement is defense-in-depth at three layers (UI, offline pre-check, server re-check); idempotency spans the full online/offline/sync/server chain with a payload-identity check; refunds have manager gating and per-batch tracking that's more rigorous than typical; allergy checking survives savepoint rollback by design; `sales_endpoints.py::list_sales` branch-scoping is the correct template, not a bug.

**Independent assessment:** the transactional core would pass a serious review on its own. The two routine-not-edge-case failure modes — silent price drift and one-click un-audited sale destruction — are what make the offline-sale-then-sync model, as it stands, unsafe to ship unchanged into a regulated, money-and-controlled-substances context.

---

## 4. Consolidated gap register

| ID | Domain | Severity | Title |
|---|---|---|---|
| S1 | Sync | **P0** | No org/branch scoping on CRR pull — cross-tenant data leak |
| S2 | Sync | **P0** | Missing push validators — cross-tenant write possible |
| P3 | Sales | **P0** | Un-gated, un-audited "Discard" destroys sale records |
| P2 | Sales | **P1** | Prescription endpoints leak org-wide PHI when assigned_branches is empty |
| A (I3) | Inventory | **P1** | PO single-object routes ignore branch scoping |
| P1 | Sales | **P1** | Offline sale prices never reconciled at sync |
| S3 | Sync | **P1** | Sales dead-lettering silently defeated — infinite retry |
| I1 | Inventory | **P1** | Duplicate batch receiving hard-rejected vs. documented spec |
| S4 | Sync | **P2** | Fragile string-matched transient/permanent error contract |
| I2 | Inventory | **P2** | Inventory endpoints missing admin bypass (too strict) |
| I4 | Inventory | **P2** | `export_inventory_excel` has no permission check |
| P4 | Sales | **P2** | Offline pricing logic duplicated, can drift from server |
| S5 | Sync | **P2** | Tombstone gaps on `branch_inventory`/`drug_batches` |
| I5 | Inventory | **P2** | No real concurrent-connection test coverage |
| I6 | Inventory | **P3** | No unique constraint backing duplicate-batch check (race) |
| P5 | Sales | **P3** | No test coverage for offline price-drift scenario |

---

## 5. Recommended approach — fixing this efficiently, not from scratch

None of this requires replacing the CR-SQLite/CRDT foundation or the transaction-processing core; both are sound. The efficient path is: **one shared authorization primitive, one sync-isolation fix, one audit-integrity fix, one pricing-reconciliation step, and a regression-test net that makes all four permanent.**

### 5.1 Multi-tenant sync isolation (closes S1, S2)

- Add an `organization_id` (and where applicable `branch_id`) resolution step in `get_changes_since()`: join each `crsql_changes` row back to its owning table's row to determine ownership, and filter server-side before returning to a client. This is a targeted fix, not a rewrite.
- For defense in depth, consider partitioning the shadow DB per organization (separate SQLite file/schema per org) so a filtering bug in one place can't leak across the partition boundary — this also bounds blast radius if S1-class bugs recur elsewhere.
- Add a validator per CRR table (extending the pattern that already exists for `branch_inventory`, correctly this time) that rejects any pushed row whose `organization_id`/`branch_id` doesn't match the authenticated user's actual scope — closes S2.
- Add a permanent regression test: two orgs, two clients, assert a full push/pull cycle never returns org B's rows to org A's client, and never accepts org A writing over org B's row IDs.

### 5.2 One authorization primitive, not eleven (closes S2's auth half, A/I3, I2, P2)

- Implement `scope_to_accessible_branches()` once (§2), migrate every router in the table above to call it, delete the hand-rolled conditionals.
- Add the "zero assigned branches, non-elevated user → empty result" test as a parametrized case run against every list/get router in CI, so this class can't reappear silently in the next new endpoint.

### 5.3 Audit-safe failure handling for sales (closes P3, S3)

- Replace the client-side `discardFailure` flip with a server-mediated "void offline sale" action requiring the same manager-approval gate as `refund_sale`/`cancel_sale`, writing a real audit-log row.
- Fix the dead-letter reset ordering in `reconcileOfflineSales()` so `attempts` is only reset with explicit admin action, never automatically at the top of every sync cycle — and surface truly-stuck sales in an admin-visible queue instead of retrying them forever.
- Replace the string-matched transient/permanent error classification (S4) with a small typed error-code enum shared between backend and frontend (a generated contract, or at minimum a single source-of-truth constants file imported by both, plus a contract test asserting the backend's actual emitted strings match what the frontend matches on).

### 5.4 Price integrity across the offline boundary (closes P1, P4, P5)

- At sync time, re-run `pricing_calculator.compute_item_pricing` server-side against the offline sale's line items and compare to what the client submitted. Within tolerance → accept as-is. Outside tolerance → don't silently accept; flag the sale for reconciliation review (a variance ledger entry) rather than either rejecting a completed, drug-already-dispensed transaction or silently absorbing the discrepancy.
- Reduce duplicate pricing logic by extracting the contract-pricing rules (exclusions, caps, floors) into a single spec both implementations are tested against with the same fixture set — doesn't require sharing a runtime, just a shared test contract.
- Add the price-drift-during-offline scenario to the existing, already-thorough `test_offline_sale_sync.py` suite.

### 5.5 Inventory correctness cleanups (closes I1, I4, I5, I6)

- Implement append-on-duplicate-batch-number as documented in `pharmacy_audit_plan.md` §5.6, with the receiving path holding the batch row lock across the check-then-write to close I6 at the same time.
- Add the missing permission/branch check to `export_inventory_excel` — small, already-scoped fix.
- Add one real concurrent-connection test harness (`asyncio.gather` across independent sessions, or a multiprocess test) for: two terminals selling the last unit of a batch; two simultaneous PO receipts; a CRR push racing a local sale on the same row. This validates the locking design that's currently only asserted, not demonstrated.

### 5.6 Process recommendation

The investigation history in this repo (spike → swap → phased migration, each closed with real-Postgres evidence) is a good model — keep using it. The regressions found here (S3, S4) came from patching specific bug reports ("sale stuck pending", "prescription retries dead-lettering") without a general transient/permanent failure taxonomy. Before the next round of "sync got stuck again" fixes, write that taxonomy down once (which error conditions are retryable, which need admin escalation, which are permanent) and make both client and server conform to it — that will prevent this specific class of regression rather than requiring another point-fix next time.

---

## 6. Deployment readiness checklist

**Blockers — must be fixed and verified before onboarding any second organization / before production go-live:**
- [ ] S1 — CRR pull scoped by organization/branch, with a cross-org isolation regression test
- [ ] S2 — Per-table push validators enforcing row ownership matches the authenticated user's org/branch
- [ ] P3 — Sale discard converted to an audited, manager-approved server action
- [ ] P2 — Prescription endpoint branch-scoping fixed (same fix as decision 0005, applied here)
- [ ] A/I3 — PO single-object endpoints branch-scoped

**High priority — fix within the first patch cycle after launch:**
- [ ] P1 — Price reconciliation at sync time, with variance flagging
- [ ] S3 — Dead-letter reset ordering fixed; stuck sales surfaced to admins, not silently retried forever
- [ ] I1 — Duplicate-batch receiving matches documented append behavior
- [ ] S4 — Typed sync error taxonomy replacing string matching

**Medium — schedule soon, not launch-blocking on their own:**
- [ ] I2 — Inventory endpoint admin bypass
- [ ] I4 — `export_inventory_excel` permission check
- [ ] P4/P5 — Shared pricing contract test + offline price-drift test coverage
- [ ] S5 — Tombstone columns on `branch_inventory`/`drug_batches`
- [ ] I5/I6 — Real concurrency test harness + batch-receipt race fix

**Gate recommendation:** do not enable multi-organization operation on shared infrastructure — even in a pilot — until the five Blockers above are fixed and the cross-org isolation test suite is green. A single-tenant pilot can currently "work" while masking S1/S2 entirely, since there's no second organization's data to leak into.

---

## 7. Sources consulted (not duplicated, only extended)

- `docs/decisions/0002-local-db-concurrency-model.md`, `0003-server-side-crdt-merge-architecture.md`, `0005-purchase-order-branch-authorization-gap.md`
- `docs/evidence/2026-07-10-crr-branch-inventory-drug-batches-real-postgres.md`, `2026-07-11-crr-all-tables-production-dispatch.md`, `2026-07-11-full-crr-and-reference-cache.md`
- `docs/investigations/crr-integration-findings.md`, `crr-phase2-discovery.md`, `crsqlite-spike-findings.md`, `rusqlite-swap-findings.md`, `sync-audit-gaps.md`, `security_audit.md`, `all_prompt.md`
- `docs/plans/offline_sync_improvement_plan.md`, `pharmacy_audit_plan.md`
- `docs/offline-sales.md`
- Recent commits: `d24d008`, `3974a03`, `c0f75ed`, `2b44ff4`, `162418e` (diffs read in full)
