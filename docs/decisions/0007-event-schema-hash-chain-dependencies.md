# 0007: Event Schema, Hash Chain, and Dependency Semantics

**Status:** Accepted
**Date:** 2026-08-12
**Related:** [0006 — Event-Sourced Sync Spine](0006-event-sourced-sync-spine.md)

## Context

ADR 0006 commits to an event-sourced sync spine. Before writing any
code, the event envelope, ordering guarantees, tamper-evidence chain,
and dependency-resolution semantics need to be locked so client, server,
and projector code can be written against a single contract.

## Decision

### Event envelope

Every event carries this envelope, regardless of aggregate type:

```
event_id       ULID           Client-generated, lexicographically sortable
aggregate_id   UUID           Target aggregate (sale_id, prescription_id, …)
aggregate_type TEXT           "sale" | "prescription" | "customer" | "stock" | …
event_type     TEXT           "SaleRecorded" | "PrescriptionCreated" | "SaleVoided" | …
schema_version SMALLINT       Payload schema version for this event_type (starts at 1)
payload        JSONB          Event-type-specific fields
dependencies   ULID[]         Other event_ids this event requires
authored_at    TIMESTAMPTZ    Wall-clock time at author site
authored_by    UUID           User id
branch_id      UUID           Author branch
org_id         UUID           Author organization
hash_self      TEXT           SHA-256 over canonical envelope + payload + hash_prev
hash_prev      TEXT           hash_self of the preceding event in the org's global log
seq            BIGINT         Server-assigned monotonic global sequence (NULL until server accept)
received_at    TIMESTAMPTZ    Server clock at accept (NULL until server accept)
```

Client-visible fields (all except `seq`, `received_at`, `hash_prev` for
the head-of-log case) are set before the event is written to the local
outbox and never mutated afterward. `seq`, `received_at`, and
`hash_prev` are assigned server-side on accept.

### ULID as event_id

Client-generated ULIDs give three properties simultaneously:

1. **No coordination** — clients can generate ids offline without
   collision risk.
2. **Lexicographic time-ordering** — outbox FIFO by `event_id` string
   is also FIFO by author time, so local projection order matches
   authoring order without a separate sequence column.
3. **Idempotency key** — the server rejects duplicates by `event_id`
   as a no-op success, making resends safe by construction.

The trailing random bits protect against multi-writer collisions at the
same millisecond on the same branch (thousands of unique ids per ms).

### Hash chain

Every accepted event carries `hash_prev` pointing to the previous
event's `hash_self` in the same organization's log. This gives:

- **Tamper-evident audit trail** — any historical mutation breaks the
  chain and is detectable by re-hashing.
- **Regulatory alignment** — pharmaceutical records need chain-of-
  custody; this is that chain, mechanically.

`hash_self` is computed as:

```
SHA-256(canonical_json({
  event_id, aggregate_id, aggregate_type, event_type,
  schema_version, payload, dependencies,
  authored_at, authored_by, branch_id, org_id, hash_prev
}))
```

Canonical JSON = keys sorted, no whitespace, UTF-8, numbers in shortest
form. Both client and server implement the same canonicalizer so the
client can verify its own outbox and the server can verify on accept.

`hash_prev` for the very first event in an organization's log is the
32-byte zero string (`"0" * 64`). Chain is per-organization, not
global — each org's log is independent.

### Dependency semantics

`dependencies` lists `event_id` values this event requires to have been
applied before its own projection can run. Examples:

- A `SaleRecorded` event depends on the `PrescriptionCreated` event for
  its rx-required items (if the prescription was authored offline in
  the same session).
- A `PrescriptionCreated` event depends on the `CustomerCreated` event
  for its customer_id (same reason).

The server-side behavior when a dependency has not yet been received:

1. Event is accepted into `event_log` (it is durable, idempotent, and
   chained).
2. Projection is **deferred** — the event is enqueued in a per-org
   `pending_projections` queue keyed by unresolved dependency ids.
3. When a missing dependency arrives and projects successfully, the
   queue is scanned and any events whose full dependency set is now
   satisfied are dispatched to their projectors.
4. There is no cascading batch failure — one deferred event never
   blocks the acceptance of subsequent events in the same push batch.

Rules the client honors when constructing dependencies:

- Only include dependencies the client authored in the current session
  and has not yet observed as acknowledged (has a server-assigned
  `seq`). A dependency the server has already applied is unnecessary.
- Cross-aggregate references that resolve against long-existing server
  state (e.g. a `drug_id` from the catalog) are **not** dependencies —
  they resolve through the projector's validator against Postgres.

### Failure classification

Server responses per event:

- **`accepted`** — appended to log, projected successfully.
- **`accepted_deferred`** — appended to log, projection deferred pending
  a dependency. Client treats as success; server will project when the
  dependency arrives.
- **`rejected_permanent`** — payload is structurally invalid or violates
  a business rule that cannot be resolved by retry (invalid org scope,
  malformed schema, referenced entity that will never exist). Client
  moves this event to a `dead_letter` state, surfaces to the user via
  the failures panel, and never retries it. The event is **not**
  appended to the log — dead-lettered events are only in the client
  outbox with a terminal error.
- **`rejected_transient`** — server-side transient (DB unavailable,
  network mid-write). Client retries with exponential backoff.

Note the shift from the current design: there is no `dependency_not_synced`
category that loops forever. A dependency that will never arrive is
detected server-side (it is not in the log and no in-flight event will
produce it) and returns `rejected_permanent` on the *dependent* event,
not another deferral.

## Rationale

- **ULID over UUID v4:** we need both offline generation and time-order
  for FIFO shipping. UUID v7 would also work; ULID picked for
  lexicographic sortability without extra library work.
- **JSONB payload over typed columns:** every aggregate/event_type
  combination has a different payload shape; typed columns would need
  a migration per new event_type. Envelope is what queries hit; payload
  is what projectors read. JSONB is the right shape for the second.
- **Hash chain per organization, not global:** organizations are the
  natural tenancy boundary; a global chain would create cross-tenant
  ordering dependencies that add complexity for no regulatory or audit
  benefit. Per-org chains are independently verifiable.
- **Server assigns `seq`, not the client:** clients can generate
  `event_id` collision-free, but a global monotonic sequence per org is
  needed for the pull cursor to be simple (`WHERE seq > ?`). Assigning
  it server-side keeps that guarantee.
- **Deferred projection over cascading batch failure:** the current
  CRR design fails a whole batch on one poison row (cursor never
  advances). Deferring the specific event that lacks a dependency,
  while accepting the rest, removes an entire class of "one bad row
  stalls everything" bugs.
- **Explicit `dead_letter` state on the client:** the client, not the
  server, owns the outbox lifecycle. A `rejected_permanent` response
  moves the event out of active retry and into a user-visible failure
  state. This matches the client-side dead-letter behavior added on
  2026-08-12 for the offline-sale retry fix.

## Consequences

- Payload evolution is by adding a new `event_type` variant or bumping
  `schema_version`. Projectors dispatch on `(event_type, schema_version)`.
  Old event versions must remain projectable indefinitely for replay.
- Hash-chain computation adds a small write-path cost per event. Server
  computes `hash_prev` at accept-time (single indexed lookup); client
  computes `hash_self` before outbox write. Both are O(1) per event.
- The `pending_projections` queue is a new server-side data structure
  that needs its own retention (drop entries whose event has been
  projected). Straightforward but must be operationally monitored.
- Projectors must be pure functions of `(event, current_read_model)` —
  no wall-clock reads, no non-deterministic behavior — so a full replay
  from `event_log` reproduces the same read model. This constrains
  projector implementations; violations break replay guarantees.
- Client-side canonical-JSON hasher must exactly match the server's
  Python implementation, byte-for-byte, or hash chains break. One
  golden-vector test suite shared by both.

## Open Items (not blocking Phase 1)

- Exact `dependencies` derivation rules per event_type — will be pinned
  in the projector implementations during Phase 1 and codified as a
  table in the sync module README.
- Whether `dead_letter` events should also be reported to the server
  for cross-branch visibility, or stay purely client-side. Default:
  purely client-side; revisit if support-desk workflows need remote
  visibility.

## Trigger for Revisit

- If `pending_projections` queue depth ever exceeds a healthy operational
  bound (e.g. sustained > 1000 entries per org), re-examine whether
  clients are over-declaring dependencies or the projection loop is
  under-scaled.
- If a real regulatory audit requires cross-organization chain
  verification, revisit the per-org chain decision.
