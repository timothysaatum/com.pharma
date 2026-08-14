# 0006: Event-Sourced Sync Spine (Supersedes 0003)

**Status:** Implemented
**Date:** 2026-08-12
**Completed:** 2026-08-14
**Supersedes:** [0003 — Server-Side CRDT Merge Architecture](0003-server-side-crdt-merge-architecture.md)

## Context

ADR 0003 introduced a server-side shadow SQLite + cr-sqlite CRDT
architecture with a mandatory shadow-to-Postgres upsert step. Its own
"Trigger for Revisit" section anticipated the failure mode we hit:

> Revisit if the shadow-SQLite + upsert step becomes an operational
> burden (e.g. drift between shadow SQLite and Postgres becomes a
> recurring incident).

Over the last month, the majority of sync bugs traced back to that
exact seam — cascading dependency rejections, shadow-row tombstone
resurrection races (`cid=-1` sentinel resends), silent FK stripping in
sale FK validation, batch-cursor stalls, and offline-sale retry loops
(fixed 2026-08-12, commit context).

The two-track split (CRR for reference tables + legacy `sync_queue` for
sales) compounds the problem: sales and their prescription dependencies
travel through different code paths with different retry semantics,
producing failures that are hard to reason about at the counter.

The system has no production users yet, so migration risk is zero and
this is the cheapest possible moment to change direction.

## Decision

Replace the current CRR + legacy queue design with a three-layer sync
architecture:

**Layer 1 — Event-sourced spine.** All transactional mutations (sales,
prescriptions, refills, stock adjustments, voids, transfers) become
immutable events with client-generated ULIDs. Clients append to a local
`event_outbox`, project into local read tables, and ship events FIFO to
the server. The server appends to a canonical `event_log` (idempotent
by `event_id`) and dispatches to per-aggregate projectors that write to
existing Postgres tables. Details in ADR 0007.

**Layer 2 — LWW with vector clocks.** Reference data with concurrent-
edit potential (customer profiles, drug catalog, pricing rules, branch
settings) uses a version vector per `(record_id, branch_id)`. Detected
conflicts land in `unresolved_conflicts` for manager review; there is
no silent last-write-wins overwrite.

**Layer 3 — Branch-owned inventory with transfer events.** Physical
stock at a branch is single-writer for that branch. Cross-branch stock
movement uses a two-phase event pair (`TransferInitiated` +
`TransferReceived`), never implicit reconciliation.

## Rationale

- **The conflict profile does not justify CRDTs.** Ghanaian pharmacies
  operate one cashier per branch at a time. cr-sqlite's automatic
  conflict resolution is powerful for genuinely concurrent multi-writer
  scenarios that this system almost never has.
- **Failures must be legible to non-engineers.** Every current sync bug
  ends in CRDT internals ("shadow row tombstoned by cid=-1, resend
  loses causality race"). A pharmacist cannot act on that. Event-
  sourced failures read as "sale X cannot sync because prescription Y
  was rejected" and are surfaceable in the failures panel with a
  concrete action.
- **Postgres is already the source of truth.** The current design keeps
  two authoritative stores (shadow SQLite + Postgres) that must be
  reconciled. Every bug in the past month lives in that seam.
  Collapsing to one authoritative store removes the entire class.
- **Regulatory audit trail is free.** Pharmaceutical records require
  chain-of-custody and multi-year retention. The eventlog *is* the
  audit trail — hash-chained for tamper detection, replayable, and
  matches what regulators already expect.
- **The pieces we care about survive unchanged.** Pricing engine,
  loyalty engine, tax calculation, and all field-level validators
  relocate into projectors as-is. This rewrite touches the sync layer,
  not the business logic.

## Consequences

- ✅ Two-track sync (CRR + legacy queue) collapsed to one — event stream
  per branch, projected into existing Postgres tables.
- ✅ cr-sqlite native extension removed from the required startup path
  (loading is now optional/warn-only); `crsql_changes`, `crsql_pack_columns`,
  `suppressed_crr_changes`, `crr_audit_uploads`, and `customer_merge_directives`
  dropped from the client schema via localDb v26 migration (2026-08-14).
  `sync_queue` retained for purchase_orders/branch_inventory push — deferred.
- ✅ `shadow_db.py`, `crr_sync_service.py`, `crr_sync_endpoints.py` deleted;
  legacy sale path in `sync_service.py` removed. `pricing_calculator.py`,
  `sale_helpers.py`, all validators, all Pydantic schemas preserved.
- ✅ Integration test suite rewritten; CRR-coupled tests deleted.
- ✅ Automatic concurrent-edit resolution for reference data replaced by
  explicit conflict detection + repair UI (ConflictsPage, Layer 2).
  Vector clocks per customer/drug/drug_category; concurrent edits land in
  `unresolved_conflicts` for manager review (2026-08-13).
- The eventlog grows monotonically; retention and snapshotting are
  addressed separately in ADR 0009.

## Trigger for Revisit

Revisit if either becomes true:

- The system starts serving genuinely multi-writer offline scenarios
  (e.g. mobile field reps editing customer records concurrently across
  branches at a rate the Layer 2 conflict UI cannot keep up with), at
  which point re-evaluate CRDTs for reference data specifically.
- Eventlog size or projection replay time hits an operational ceiling
  that snapshotting (ADR 0009) alone does not resolve.

## References

- Implementation plan: [`docs/plans/offline-sync-rewrite-plan.md`](../plans/offline-sync-rewrite-plan.md)
- Event envelope, hash chain, dependencies: ADR [0007](0007-event-schema-hash-chain-dependencies.md)
- Push/pull endpoint contracts: ADR [0008](0008-sync-endpoint-contracts.md)
- Retention & snapshotting: ADR [0009](0009-event-retention-snapshotting.md)
