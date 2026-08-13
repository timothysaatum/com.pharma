# Offline Sync Rewrite — Event-Sourced Spine + Vector-Clock LWW

**Author:** Design proposal
**Status:** Draft — awaiting Phase 0 kickoff
**Generated:** 2026-08-12

---

## Context

The current sync layer has two tracks:

- **CRR (cr-sqlite CRDT)** — customers, prescriptions, drugs, categories,
  branch inventory, price contracts, audit logs.
- **Legacy sync_queue** — sales only.

Both tracks share a server-side shadow SQLite (`shadow_db.py`) that must
be reconciled with Postgres. The seam between shadow and Postgres, and
the interaction between the two tracks, is the source of most sync bugs
we've fixed to date (see git log for CRR-related fixes in the last month,
including cascading dependency rejections, shadow-row tombstone
resurrection races, silent FK stripping, and the offline-sale retry loop
patched on 2026-08-12).

**Constraints for this rewrite:**

- No production users yet — no data migration burden.
- Preserve business logic: `pricing_calculator.py`, `sale_helpers.py`,
  all CRR/legacy validators, all schemas.
- Multi-branch, truly offline-first (Ghanaian pharmacies, patchy mobile
  data).
- Goal: **reliable + debuggable**, not maximally fast.

---

## Target Architecture

Three layers, each sized to the concurrency profile of its data type.

### Layer 1 — Event-sourced spine (transactional records)

Applies to: sales, prescriptions, refills, stock adjustments, voids,
transfers.

Every mutation is an immutable event:

```
event_id       ULID (client-generated, lexicographically sortable)
aggregate_id   UUID of the target aggregate (sale_id, prescription_id, …)
aggregate_type "sale" | "prescription" | "stock_adjustment" | …
event_type     "SaleRecorded" | "PrescriptionCreated" | …
payload        JSONB
dependencies   ULID[]  (other events this one requires)
authored_at    TIMESTAMPTZ
authored_by    UUID (user)
branch_id      UUID
org_id         UUID
hash_prev      TEXT  (previous event hash for tamper-evident chain)
```

- Client appends to local `event_outbox` → projects into local read model
  → ships events FIFO.
- Server appends to canonical `event_log` (idempotent by `event_id`) →
  projects into existing Postgres tables.
- Zero merge conflicts — events don't overlap.
- Regulatory chain-of-custody is the log itself.

### Layer 2 — LWW + vector clocks (reference data)

Applies to: customer profiles, drug catalog, pricing rules,
branch settings.

- Vector clock per `(record_id, branch_id)` — concurrent edits are
  *detected*, not silently overwritten.
- On conflict: preserve both versions in `unresolved_conflicts` table,
  surface in repair UI. Manager decides.

### Layer 3 — Branch-owned inventory + transfer events

Applies to: `branch_inventory`, `drug_batches`.

- Branch stock is single-writer (that branch). Sales debit locally,
  never conflict.
- Cross-branch movement is a two-phase event pair: `TransferInitiated`
  (source) + `TransferReceived` (destination).

---

## Phase 0 — Design & ADR (3–5 days)

**Goal:** lock the schemas and protocol before writing production code.

| # | Deliverable |
|---|---|
| 0.1 | ADR: event schema, hash chain, dependency semantics |
| 0.2 | ADR: push/pull endpoint contracts (REST vs WebSocket) |
| 0.3 | ADR: retention & snapshotting policy |
| 0.4 | Migration SQL: `event_log`, `event_outbox`, `unresolved_conflicts` |
| 0.5 | TypeScript + Python types for event envelope |

**Decisions to lock:**

1. **Push transport** — REST batched (start here, simple) vs WebSocket
   streaming (lower latency, harder to reason about).
2. **Event storage shape** — Postgres JSONB payload (flexible, queryable
   with `->>` operators) vs typed columns per aggregate (rigid, faster
   queries, more migrations).
3. **Retention** — keep events forever (regulatory + full replay) vs
   snapshot + archive after N months.

**Exit criterion:** all three ADRs signed off; schemas reviewed;
no code written yet.

---

## Phase 1 — Server eventlog + projections (2 weeks)

**Goal:** server can accept events, append to log, project into existing
Postgres tables. Validators and pricing engine reused unchanged.

| # | Task |
|---|---|
| 1.1 | Create `event_log` table with hash-chain trigger |
| 1.2 | `POST /api/v1/sync/events` endpoint — batch append, idempotent by `event_id` |
| 1.3 | `GET /api/v1/sync/events?after=<seq>&aggregate_types=…` — cursor-based pull |
| 1.4 | Event router: append → dispatch to per-aggregate projector |
| 1.5 | `SaleProjector` — reuses `pricing_calculator`, `sale_helpers`, existing sale validators |
| 1.6 | `PrescriptionProjector` — reuses prescription validators |
| 1.7 | `CustomerProjector` — reuses customer validators |
| 1.8 | `StockProjector` — inventory debits, batch allocations |
| 1.9 | Dependency queue: if event A depends on B not yet received, park A; replay when B lands |
| 1.10 | Poison-event quarantine: after N projection retries, move to `event_dead_letter` with typed error |
| 1.11 | Integration tests per projector (parallel to existing `test_sync_items.py`) |

**Exit criterion:** server accepts events for all aggregate types, projects
into Postgres correctly, existing pricing/validator tests still pass
under the new call site.

---

## Phase 2 — Client outbox + local projections (2 weeks)

**Goal:** client stops writing directly to CRR/legacy tables. All
mutations become events. UI read path unchanged.

| # | Task |
|---|---|
| 2.1 | Create `event_outbox` table in local SQLite |
| 2.2 | Local event router mirroring server's — same projector code where possible |
| 2.3 | Refactor `recordSale`, `voidSale`, `refundSale` → emit events |
| 2.4 | Refactor `createPrescription`, `updatePrescription`, `refillPrescription` → emit events |
| 2.5 | Refactor `saveCustomer` write paths → emit events |
| 2.6 | Refactor stock adjustment / transfer paths → emit events |
| 2.7 | New sync engine: `pushEvents()` (FIFO by outbox ID) + `pullEvents()` (cursor by server seq) |
| 2.8 | Ack handling: server assigns global sequence, client marks outbox rows shipped |
| 2.9 | Retry policy: exponential backoff on network errors; typed error → quarantine + surface in failures panel |
| 2.10 | Vitest coverage for new sync engine |

**Exit criterion:** client can go offline, record a full sale
(customer + prescription + rx-required sale), come online, and see all
three land on the server in one push cycle with no manual intervention.
This is the exact scenario the current 2026-08-12 fix addresses — under
the new design, it should be structurally impossible to fail here.

---

## Phase 3 — LWW + vector clocks (1 week)

**Goal:** concurrent edits to reference data no longer silently overwrite
each other.

| # | Task |
|---|---|
| 3.1 | Add `version_vector` JSONB column to customers, drugs, drug_categories, price_contracts, branch_settings |
| 3.2 | Update = bump `(record_id, branch_id)` component of vector, propagate |
| 3.3 | Conflict detection: incoming vector concurrent with local → write to `unresolved_conflicts` |
| 3.4 | Repair UI skeleton: list conflicts, show diff, "keep local / keep remote / merge" |
| 3.5 | Migration path for existing customer/drug records (assign initial vector on first write) |

**Exit criterion:** simulated concurrent customer-profile edits from two
branches produce a conflict row surfaced in the repair UI, not a silent
last-write-wins overwrite.

---

## Phase 4 — Rip out CRR + legacy queue (3–5 days)

**Goal:** delete the old code so it can't come back to haunt us.

| # | Task |
|---|---|
| 4.1 | Delete `backend.laso/app/services/sync/shadow_db.py` |
| 4.2 | Delete `backend.laso/app/services/sync/crr_sync_service.py` |
| 4.3 | Delete legacy `_push_sale` path from `sync_service.py`; keep read-only helpers if reused |
| 4.4 | Remove cr-sqlite native extension + client dependency |
| 4.5 | Drop CRR tables from client schema (`crsql_changes`, `crsql_pack_columns`, etc.) |
| 4.6 | Drop `sync_queue` from client schema |
| 4.7 | Delete CRR-specific integration tests; retain business-logic tests |
| 4.8 | Update CLAUDE.md / architecture docs to reflect new sync layer |

**Explicitly preserved:**

- `app/services/sales/pricing/pricing_calculator.py`
- `app/services/sales/utils/sale_helpers.py`
- All Pydantic schemas under `app/schemas/`
- All validators (`_validate_customer`, `_validate_prescription`,
  `_validate_branch_inventory`, `_validate_drug_batch`,
  `_validate_and_fix_sale_fks`) — relocated into projectors.
- Loyalty engine, discount engine, tax calculation.

**Exit criterion:** grep for `crsql`, `shadow_db`, `sync_queue` returns
zero hits in production code. All CI green.

---

## Timeline & Effort

| Phase | Duration | Cumulative |
|---|---|---|
| 0 — Design & ADR | 3–5 days | 1 week |
| 1 — Server eventlog + projections | 2 weeks | 3 weeks |
| 2 — Client outbox + local projections | 2 weeks | 5 weeks |
| 3 — LWW + vector clocks | 1 week | 6 weeks |
| 4 — Rip out CRR + legacy | 3–5 days | ~6.5 weeks |

Assumes one focused engineer. Parallelizable across two engineers by
splitting client/server after Phase 0.

---

## Risks

| Risk | Mitigation |
|---|---|
| Sale projection is the hardest (inventory debits, batch allocations, discount + loyalty side effects) | Budget extra time for `SaleProjector` in Phase 1. Port existing sale-recording tests first as the acceptance suite. |
| Test suites are tightly coupled to shadow DB semantics — rewriting them is real work | Treat test rewrite as a first-class Phase 1/2 deliverable, not incidental. |
| UI mutation paths may bypass sync layer in some places (direct table writes) | Audit in Phase 0. Any direct-write path becomes an event-emission path in Phase 2. |
| Event schema evolution — adding fields later requires care | JSONB payload + `event_type` version suffix (`SaleRecorded.v1`, `.v2`) buys forward-compat without migrations. |
| Hash-chain performance under high write volume | Compute chain server-side after commit, not synchronously in the write path. If ever hot, batch-chain per second. |

---

## What Success Looks Like

At the end of Phase 4:

1. Recording a sale offline while creating a new customer + prescription
   in the same session — then coming online — results in all three
   landing on the server in one push cycle with no manual intervention
   and no retry loops.
2. Two branches concurrently editing the same customer's chronic
   condition list produces a visible conflict for a manager to resolve,
   not a silent overwrite.
3. When something *does* fail, the error surfaced to the pharmacist is
   actionable ("Sale can't sync — prescription X was rejected, click to
   fix") not a CRDT internals leak ("shadow row tombstoned by cid=-1").
4. The sync layer has one code path per data flow, not two, and no
   shadow database.
5. The regulatory audit trail is the eventlog itself — hash-chained,
   tamper-evident, replayable.

---

## Open Questions for Kickoff

1. Which of the three Phase 0 decisions (transport, storage shape,
   retention) do you have strong opinions on already?
2. Solo or two-engineer effort? Timeline halves if split cleanly after
   Phase 0.
3. Any near-term feature commitments that need to ship on the *current*
   sync layer before we start Phase 1?
