# Conserved Quantities — Target Architecture for the Offline-First POS

**Author:** Design proposal
**Status:** Proposal — Accepted
**Generated:** 2026-08-14

Written after provisioning the stack on PostgreSQL, running both test suites,
and tracing four reported UI bugs to nine underlying defects. Findings
referenced below are from that session.

Rendered version: <https://claude.ai/code/artifact/bd615d8f-78e8-456e-b242-b4adfd15fe18>

---

## 1. The constraint everything follows from

Almost every hard bug in this codebase comes from one mismatch: general-purpose
sync machinery applied to a domain whose central rule is a conservation law.

> **Invariant.** Stock is conserved. Two terminals that each sold the last five
> units cannot be merged into a correct state — the medicine is already in
> patients' hands.

Sync frameworks are built to merge. CRDTs merge sets, counters and text
beautifully, and the first architecture here reached for cr-sqlite on exactly
that promise (see `0003-server-side-crdt-merge-architecture.md`). The second
reached for an event-sourced spine (`0006-event-sourced-sync-spine.md`). Both
are reasonable engines. Neither answers the question that actually matters,
which is **who is allowed to decide that a unit has left the shelf**.

When no component owns that decision, the system defaults to letting the client
make it and the server record it. That is the shape of the current code: the
till chooses which batch to draw from, and the projector decrements whatever it
is told. Every defect found downstream — an unscoped batch update, a missing
expiry guard, a cart reading a different number than the list beside it — is a
consequence of that one unassigned responsibility.

This plan therefore does not start with a sync mechanism. It starts by naming
the invariants, then chooses machinery that can enforce them.

---

## 2. Six principles

**P1 — Partition what you cannot merge.**
Conserved resources get an owner, not a merge function. A branch owns its
stock; a terminal leases a slice of it. Offline selling draws only on what the
terminal already holds, so there is nothing to reconcile afterwards.

**P2 — Events carry intent; servers derive state.**
A till says "dispense 5 of this drug", never "decrement batch 7f3a by 5".
Allocation is a decision requiring the full picture — expiry, FEFO order,
tenancy — and only the server has it.

**P3 — One owner per domain rule.**
"Which stock is sellable" is currently implemented in six places across two
languages (`sync_service.py`, `projectors/sale.py`, `inventory_service.py`,
`inventory_deductor.py`, `localRead.ts`, `offlineSalesManager.ts`). Rules with
several implementations do not stay in agreement.

**P4 — The replica has one ingestion path.**
Local state arrives from the sync stream and nowhere else. Two writers filling
different tables from different triggers do not drift occasionally — they drift
by construction.

**P5 — Never render a state you have not verified.**
A green tick beside "Never" is worse than a blank screen: it converts an unknown
into a false assurance. Unverified is its own state and must look like one.

**P6 — Test against the engine you deploy.**
The suite builds its schema from ORM models on SQLite; production runs
migrations on PostgreSQL. The two differ in column names and column types,
which is precisely why three broken projector statements shipped green.

---

## 3. The architecture

### Separate what happened from what it cost

A **dispense** is a clinical act: medicine left the shelf and entered a
patient's hands. A **sale** is a commercial one. Today they are a single
record, which is why refunds are awkward — reversing the money should not imply
the tablets came back. Modelling them separately also makes the regulatory
story clean, because dispensing of controlled substances is what the regulator
asks about, not revenue.

### The event log stays

Append-only, hash-chained, server-sequenced. This part of the current work is
sound and worth keeping. Events are historical facts, so they never conflict;
only *derived state* conflicts, and derived state is rebuildable. The change is
to the payload: intent in, allocation out.

### Stock leases

The load-bearing idea. While a terminal has connectivity it continuously holds
a **lease**: an exclusive claim on a quantity of specific batches, with an
expiry time. The server grants leases atomically, so no two terminals can hold
the same unit.

- Online, sales draw from the branch pool and the lease is a formality.
- Offline, a terminal may sell **only** from its lease. Overselling becomes
  impossible rather than detectable.
- Lease size tracks observed sales velocity, so a busy till holds more headroom
  than a quiet one.
- Leases expire. A terminal that vanishes returns its units automatically,
  without an administrator intervening.

The honest cost: a terminal offline long enough to exhaust its lease must stop
selling that drug. That is correct behaviour — the software declining to
promise stock it cannot prove exists — but it is a business decision worth
making deliberately rather than discovering during a power cut.

### Sellable quantity is published, not recomputed

The server computes sellable quantity per branch and drug (unexpired,
unreserved, minus outstanding leases) and emits it on the sync stream like any
other projection. Clients read the number. No client performs expiry
arithmetic, so the list, the cart and the checkout cannot disagree.

### The client replica

A device holds a complete, consistent slice of its own branch, populated solely
by sync — not a cache assembled from whichever screens the pharmacist happened
to open. If sync has not delivered something, the device does not have it and
says so.

---

## 4. Migration path

Nothing here requires a rewrite. The event spine, the dead-letter quarantine
and the conflicts UI are all reusable. The sequence matters more than the
speed: each phase leaves the system shippable, and the early phases exist to
make the later ones safe.

| Phase | Goal | Blocking? | Rough size |
|-------|------|-----------|------------|
| 0 | Close the tenancy hole; get one device to sync green | Yes — everything downstream assumes it | days |
| 1 | One published definition of sellable quantity | No | 1 week |
| 2 | Intent-based events; server-side FEFO allocation | Yes for leases | 2–3 weeks |
| 3 | Single ingestion path into the local replica | No | 1 week |
| 4 | Stock leases for safe offline selling | Only if multi-terminal | 3–4 weeks |
| 5 | Pilot at one branch with daily reconciliation | — | 2 weeks |

### Phase 0 — Make it honest

Add `branch_id` and `organization_id` predicates to the batch deduction in
`projectors/sale.py`. Today a client-supplied `batch_id` can decrement another
branch's or another tenant's stock: the deduction is scoped only by batch id,
quantity and (since this session) expiry. Add a schema-parity check so models
and migrations cannot drift again, and move the suite onto PostgreSQL so it
exercises the engine production runs.

Then get a single device to complete one sync, end to end. Everything after
this assumes a working stream; without it, Phase 3 in particular would remove
the fallback currently keeping the POS populated at all.

**Exit criteria** — a cross-tenant batch reference is rejected, with a test
proving it; the suite runs on PostgreSQL in CI; one device shows a real
last-synced timestamp.

### Phase 1 — One rule, one owner

Compute sellable quantity once server-side, expose it on the inventory
projection and the sync stream, and strip the expiry arithmetic out of the six
places that reimplement it. The client stops deriving and starts reading.

**Exit criteria** — grepping for expiry comparisons returns exactly one
implementation; the POS list and cart read the same field.

### Phase 2 — Invert the trust

Change the sale event payload to carry drug and quantity rather than batch
allocations. The projector runs the same FEFO allocation the online path
already performs, enforcing expiry, tenancy and ordering by construction.

Receipts still need lot numbers, so the till sends a *provisional* allocation
for printing. It is display-only; the server re-allocates authoritatively and
records any divergence as a conflict for review.

**Exit criteria** — no server code path reads a client-chosen `batch_id` as
authoritative; a replayed event allocates identically regardless of what the
client proposed.

### Phase 3 — One writer

Delete the REST-response-to-SQLite writes (`cacheBranchInventoryRows` and
friends). All local rows arrive via sync. This is what makes the `noBatchData`
fallback added in `d086448` unnecessary — that workaround exists only to paper
over the dual-writer design and should be deleted in the same commit.

**Exit criteria** — exactly one module writes to the local database; the
fallback is gone and the POS still sells on a cold start after first sync.

### Phase 4 — Partition

Introduce the lease as a first-class aggregate: grant, consume, renew, expire.
Terminals acquire in the background while online and surface their offline
headroom to the cashier — "40 more units sellable offline" is information a
pharmacist can act on.

Build this only if branches genuinely run more than one till offline. A
single-terminal branch does not need it, and the complexity is real.

**Exit criteria** — a simulation running two terminals through a network
partition never oversells, across thousands of randomised runs.

### Phase 5 — Prove it

Server stock against local stock against a physical count, every day, at one
branch for a fortnight. Drift between the first two is the canary for sync
defects, and you want to find it with one pharmacist you can telephone.

**Exit criteria** — two consecutive weeks with zero unexplained drift and an
empty dead-letter queue.

---

## 5. The test strategy that would have caught all of it

Each row maps to a specific defect found this week, which is the only real
argument for adding a test.

| Test | Defect it would have caught |
|------|------------------------------|
| Schema parity: every migration column exists on its model, and vice versa | `version_vector` in migration `b2c3d4e5f6a7` but on no model — autogenerate would have emitted a `DROP COLUMN` |
| Suite runs on PostgreSQL, the deployed engine | Three projector bugs: a non-existent column (`synced_at`), ISO strings where asyncpg demands datetimes, a `varchar = uuid` comparison |
| Conservation property test: randomised partitions, assert units in equals units out | Offline oversell, and expired-batch dispensing on the replay path |
| Cross-tenant fuzz: reference another org's identifiers on every write path | The unscoped batch deduction |
| Single-source assertion on derived figures | The POS list reading 220 while the cart beside it read 0 |
| UI state tests for never-synced, stale and offline | A green tick beside "Never" |
| Collection smoke test in CI | A suite that could not collect at all, so ~4,000 lines of new tests never ran |

---

## 6. What to keep, what to retire

**Keep**

- The append-only event log and its hash chain
- Server-assigned sequencing
- The dead-letter quarantine — right shape, needs a human review queue
- The conflicts page as where unresolvable states surface
- The ADR discipline in `docs/decisions`
- Server-side re-validation at commit, which already works correctly

**Retire**

- Client-chosen batch allocation
- Opportunistic REST-to-SQLite caching
- Five of the six sellable-quantity implementations
- The `noBatchData` fallback, once Phase 3 lands
- SQLite as the test engine
- Any UI state reporting health it has not confirmed

---

## 7. Risks worth naming

**Leases add real complexity.** A new aggregate, a renewal loop and an expiry
sweeper. If every branch runs one till, skip Phase 4 entirely — the ceremony
would buy nothing.

**Provisional receipts can differ from the authoritative allocation.** A
printed lot number may not match what the server ultimately decremented. For
most pharmacies this is immaterial; where lot traceability is a regulatory
requirement it is not, and the till should then refuse to print a lot number
offline rather than print one that may be wrong.

**Phase 2 changes the event schema.** Devices in the field will emit the old
shape, so the projector must accept both for one release cycle and clients must
be forced to upgrade before the compatibility window closes.

**Phase 3 removes a crutch.** Deleting opportunistic caching before sync is
reliably green would leave the POS empty. That is why Phase 0 is blocking and
not merely recommended.

---

## Open question carried forward

`test_push_sale_rejects_rx_drug_without_valid_prescription` currently fails:
the rewrite returns `dependency_not_synced` (transient, retries) where the test
expects `PERMANENTLY_REJECTED` (dead-letter immediately). Deferring is right
for a prescription that has not synced yet; rejecting is right for one that
never existed. This is a regulated-dispensing semantics decision and is
deliberately left open.
