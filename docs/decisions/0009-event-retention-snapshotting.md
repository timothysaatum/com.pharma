# 0009: Event Retention and Snapshotting Policy

**Status:** Accepted
**Date:** 2026-08-12
**Related:** [0006 — Event-Sourced Sync Spine](0006-event-sourced-sync-spine.md), [0007 — Event Schema, Hash Chain, and Dependency Semantics](0007-event-schema-hash-chain-dependencies.md)

## Context

The event-sourced spine (ADR 0006) produces an append-only `event_log`
per organization. Over time this table grows monotonically. Two
questions must be answered before Phase 1 code is written:

1. **How long are events retained live in the primary Postgres table?**
2. **Is a projector replay from `event_log` ever bounded, or must it
   process all history?**

The domain adds a regulatory constraint: pharmaceutical records
(prescriptions, controlled-substance dispenses, sales of scheduled
drugs) typically require multi-year retention under Ghana FDA and
similar jurisdictions. Losing eventlog data is not a storage-
optimization decision; it is a compliance decision.

The system has no production users yet, so there is no existing volume
to accommodate. This ADR sets a policy that is safe by default and
defers optimization until real volume demands it.

## Decision

### Retention (live table)

For the first 12 months of production operation:

- **All events retained in the primary `event_log` table indefinitely.**
- No archival job runs. No events are deleted or moved.
- The table is partitioned by `org_id` and month for query performance,
  but partitions are not detached or archived automatically.

At the 12-month mark, or when the primary table exceeds an operational
threshold (**500 GB per organization** or **50M events per organization**,
whichever comes first), the snapshotting design below activates.

Regardless of whether snapshotting is active, the raw eventlog is
**never** deleted from durable storage — it moves to cold storage but
remains recoverable indefinitely. This is a compliance floor, not a
storage optimization.

### Snapshotting (design, not built in Phase 0)

The following design is committed to the ADR now so Phase 1 code can
be written with snapshot-compatibility in mind, but the snapshotting
machinery itself is **not built during the initial rewrite**.

**Read-model snapshots** (per-aggregate, per-organization):

- Each aggregate type (sale, prescription, customer, stock,
  branch_inventory) periodically emits a `snapshot` row into
  `aggregate_snapshots`, capturing the projected state at a specific
  `event_log.seq`.
- Snapshots are written by the same projector code that would replay
  from scratch; they are a caching layer over projection, not a
  parallel truth.
- A projector replay resumes from the most recent snapshot for that
  aggregate + org, then applies events with `seq > snapshot_seq`.

**Cold-storage archive** (per-organization):

- Once a month's partition of `event_log` is fully snapshotted (every
  aggregate touched by events in that partition has a snapshot at or
  after the partition's max `seq`), the partition becomes eligible for
  archive.
- Archived partitions move to object storage (S3/GCS/equivalent) as
  compressed JSONL, one file per `(org_id, year, month)`.
- Live table retains the last 12 months of partitions plus the current
  in-progress partition. Older partitions are readable via a restore
  procedure but not queried in the hot path.

**Restore procedure** (documented, tested annually):

- Restore an archived partition by streaming the JSONL back into a
  temporary `event_log_restore` table, then attaching it as a partition
  or querying it in place. Restore is expected to be rare (auditor
  request, forensic investigation) and does not need to be sub-second.

### Projector replay policy

Every projector in the system must support replay from `event_log.seq
= 0` for its aggregate type. This is enforced by:

- A CI test per projector that replays a canned event fixture from
  `seq=0` and asserts a known read-model state.
- No projector may read wall-clock time, non-deterministic external
  state, or randomness. Any state a projector needs is either in the
  event payload, in prior read-model state, or in configuration data
  that is versioned separately.

## Rationale

- **No users yet means no storage pressure.** The single biggest
  mistake we could make in Phase 0 is optimizing for storage before we
  know what shape the data actually takes at scale. Defer.
- **12 months is a reasonable "we'll see" horizon.** Enough time to
  observe real event volume per pharmacy, real query patterns, real
  regulatory requests. Not so long that we're painting ourselves into
  a corner if volume grows faster than expected.
- **Snapshots as a design commitment, not an implementation.** Writing
  projectors that are replay-safe from `seq=0` costs almost nothing at
  design time. Retrofitting snapshot-compatibility into projectors
  that assumed continuous state is very expensive. Do the cheap
  discipline now; defer the machinery until it earns its keep.
- **Cold-storage archive is a data-durability policy, not a query-
  performance policy.** Regulators care that the eventlog exists and
  can be reconstructed. They do not care about query latency. Cold
  storage satisfies the first without the cost profile of the second.
- **Per-org partitioning from day one.** Tenant isolation, backup
  granularity, and eventual archival granularity are all served by
  the same partitioning scheme. Adding it later is a schema migration
  under load; adding it now is free.
- **Explicit "never delete" policy.** Making this a design invariant,
  not an implementation detail, prevents a well-meaning future
  optimization from creating a compliance incident.

## Consequences

- Phase 1 sets up `event_log` partitioned by `(org_id, month)` from
  the first migration. No archival code ships.
- Phase 1 projectors are written and CI-tested for replay from `seq=0`.
- A CI test per projector asserts no wall-clock or non-deterministic
  reads (grep-level check for `datetime.now`, `time.time`, `random`,
  `uuid.uuid4` inside projector modules — allowed only in specific
  audited helpers).
- `aggregate_snapshots` table schema is included in the Phase 1
  migration but no snapshotting job runs. The table is empty and
  unqueried until snapshotting activates.
- Operational monitoring watches `event_log` size per org; alert
  thresholds at 250 GB (early warning) and 500 GB (snapshotting
  activation trigger) per org.
- Restore-from-archive is documented in a runbook created when
  snapshotting activates, not before.

## Trigger for Revisit

Revisit this ADR when any of the following becomes true:

- 12 months of production operation elapse.
- Any organization's `event_log` partition size crosses 500 GB or 50M
  events.
- A regulator issues guidance on maximum on-line retention that
  conflicts with the "keep everything hot" default.
- Projection replay time for a single aggregate from `seq=0` exceeds
  60 seconds in production — this is the point where snapshotting
  starts earning its keep operationally.

At revisit time, the snapshotting design above is the presumed
implementation path unless something has changed materially.
