-- =============================================================================
-- Phase 0 artifact: server-side Postgres schema for the event-sourced sync spine
-- =============================================================================
-- Canonical schema for the tables introduced by ADRs 0006, 0007, 0009.
-- This file is the reference; Phase 1 translates it into an alembic revision.
-- Everything here is additive — no existing tables are dropped in Phase 0.
--
-- Related ADRs:
--   0006 — Event-Sourced Sync Spine
--   0007 — Event Schema, Hash Chain, and Dependency Semantics
--   0008 — Sync Push/Pull Endpoint Contracts
--   0009 — Event Retention and Snapshotting Policy
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. event_log — the canonical append-only log
-- -----------------------------------------------------------------------------
-- Partitioned by (org_id, month(received_at)) from day one per ADR 0009.
-- The parent is declared LIST partitioned by org_id; per-org monthly partitions
-- are created lazily (either by trigger or by a scheduled migration job).
--
-- seq is a per-org monotonic BIGINT, assigned at accept-time. It is NOT a
-- global sequence — clients pull with a per-org cursor.
-- -----------------------------------------------------------------------------

CREATE TABLE event_log (
    event_id        TEXT           NOT NULL,           -- ULID (26 chars)
    org_id          UUID           NOT NULL,
    seq             BIGINT         NOT NULL,           -- per-org monotonic
    aggregate_id    UUID           NOT NULL,
    aggregate_type  TEXT           NOT NULL,           -- 'sale' | 'prescription' | ...
    event_type      TEXT           NOT NULL,           -- 'SaleRecorded' | ...
    schema_version  SMALLINT       NOT NULL DEFAULT 1,
    payload         JSONB          NOT NULL,
    dependencies    TEXT[]         NOT NULL DEFAULT '{}',  -- ULID[]
    authored_at     TIMESTAMPTZ    NOT NULL,
    authored_by     UUID           NOT NULL,
    branch_id       UUID           NOT NULL,
    hash_self       TEXT           NOT NULL,           -- SHA-256 hex (64 chars)
    hash_prev       TEXT           NOT NULL,           -- SHA-256 hex, zeros for org's first
    received_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    PRIMARY KEY (org_id, event_id),
    UNIQUE       (org_id, seq)
) PARTITION BY LIST (org_id);

-- Pull cursor index — every pull is WHERE org_id = ? AND seq > ? ORDER BY seq.
CREATE INDEX event_log_pull_cursor_idx
    ON event_log (org_id, seq)
    INCLUDE (aggregate_type, event_type);

-- Aggregate-scoped projector replay — WHERE org_id = ? AND aggregate_id = ?
-- ORDER BY seq is how snapshots + replay resume.
CREATE INDEX event_log_aggregate_replay_idx
    ON event_log (org_id, aggregate_id, seq);

-- Aggregate-type filter for typed replay ("replay all sale events for this org").
CREATE INDEX event_log_type_seq_idx
    ON event_log (org_id, aggregate_type, seq);

COMMENT ON TABLE  event_log IS
    'Append-only canonical event log per ADR 0006/0007. Never UPDATE or DELETE '
    'rows here — retention is governed by ADR 0009.';
COMMENT ON COLUMN event_log.event_id IS
    'Client-generated ULID. Unique per organization (PK includes org_id).';
COMMENT ON COLUMN event_log.seq IS
    'Per-org monotonic sequence assigned at accept-time. Client pull cursor.';
COMMENT ON COLUMN event_log.hash_prev IS
    'hash_self of the preceding event in this org''s log. All-zeros for the '
    'org''s very first event. See ADR 0007 for canonical-JSON hash rule.';


-- -----------------------------------------------------------------------------
-- 2. pending_projections — deferred-projection queue (per ADR 0007)
-- -----------------------------------------------------------------------------
-- An accepted event whose declared dependencies are not yet projected sits
-- here until every dependency lands. When a dependency is projected, the
-- projector service scans this table for rows whose full dep set is satisfied
-- and dispatches them.
--
-- One row per (org_id, event_id) — event may appear in the log AND here
-- simultaneously; presence here means "log has it, projection deferred."
-- -----------------------------------------------------------------------------

CREATE TABLE pending_projections (
    org_id                UUID           NOT NULL,
    event_id              TEXT           NOT NULL,
    aggregate_type        TEXT           NOT NULL,
    unresolved_deps       TEXT[]         NOT NULL,     -- shrinks as deps land
    first_deferred_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    last_evaluated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    evaluation_attempts   INTEGER        NOT NULL DEFAULT 0,

    PRIMARY KEY (org_id, event_id),
    FOREIGN KEY (org_id, event_id) REFERENCES event_log(org_id, event_id)
        ON DELETE CASCADE
);

-- When any event projects, we scan: which pending rows had it in unresolved_deps?
-- GIN index on the array supports @> and && cheaply.
CREATE INDEX pending_projections_unresolved_gin
    ON pending_projections USING GIN (unresolved_deps);

-- Operational monitoring — ADR 0007 says alert if any org's queue depth > 1000.
CREATE INDEX pending_projections_org_idx
    ON pending_projections (org_id);

COMMENT ON TABLE pending_projections IS
    'Deferred-projection queue for events whose dependencies have not yet been '
    'projected. An entry here means the event is in event_log (durable) but '
    'its projector has not run. See ADR 0007.';


-- -----------------------------------------------------------------------------
-- 3. aggregate_snapshots — read-model snapshots (schema-only in Phase 0)
-- -----------------------------------------------------------------------------
-- Committed to schema so Phase 1 projectors are written snapshot-compatible,
-- but no snapshotting job runs until an org's event_log crosses the 12-month
-- or 500-GB / 50M-event threshold from ADR 0009.
-- -----------------------------------------------------------------------------

CREATE TABLE aggregate_snapshots (
    org_id            UUID           NOT NULL,
    aggregate_type    TEXT           NOT NULL,
    aggregate_id      UUID           NOT NULL,
    snapshot_seq      BIGINT         NOT NULL,          -- max event_log.seq folded
    schema_version    SMALLINT       NOT NULL,          -- projector schema version
    state             JSONB          NOT NULL,          -- serialized read-model
    created_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    PRIMARY KEY (org_id, aggregate_type, aggregate_id, snapshot_seq)
);

-- "Latest snapshot for this aggregate" — replay reads this to resume.
CREATE INDEX aggregate_snapshots_latest_idx
    ON aggregate_snapshots (org_id, aggregate_type, aggregate_id, snapshot_seq DESC);

COMMENT ON TABLE aggregate_snapshots IS
    'Projector state checkpoints per ADR 0009. Empty until snapshotting '
    'activates. Every projector must be written to resume from these + '
    'apply events with seq > snapshot_seq.';


-- -----------------------------------------------------------------------------
-- 4. unresolved_conflicts — LWW + vector-clock repair queue (Layer 2)
-- -----------------------------------------------------------------------------
-- When two branches concurrently edit the same reference-data row (customer
-- profile, drug catalog entry, pricing rule, branch setting) and their
-- version vectors are concurrent (neither dominates), both versions are
-- preserved here for manager review instead of silently overwriting.
--
-- Not touched by Layer 1 (event-sourced spine). Populated by Layer 2 handlers
-- in Phase 3.
-- -----------------------------------------------------------------------------

CREATE TABLE unresolved_conflicts (
    id                UUID           NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID           NOT NULL,
    table_name        TEXT           NOT NULL,         -- 'customers' | 'drugs' | ...
    record_id         UUID           NOT NULL,
    local_version     JSONB          NOT NULL,         -- {branch_uuid: counter, ...}
    local_state       JSONB          NOT NULL,         -- row snapshot at conflict
    local_authored_by UUID           NOT NULL,
    local_branch_id   UUID           NOT NULL,
    local_authored_at TIMESTAMPTZ    NOT NULL,
    remote_version    JSONB          NOT NULL,
    remote_state      JSONB          NOT NULL,
    remote_authored_by UUID          NOT NULL,
    remote_branch_id  UUID           NOT NULL,
    remote_authored_at TIMESTAMPTZ   NOT NULL,
    detected_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ,
    resolved_by       UUID,
    resolution        TEXT,                            -- 'keep_local' | 'keep_remote' | 'merged'
    resolved_state    JSONB,                           -- post-merge state if resolution='merged'

    CHECK (resolution IS NULL OR resolution IN ('keep_local','keep_remote','merged')),
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL)),
    CHECK ((resolved_at IS NULL) = (resolution IS NULL))
);

CREATE INDEX unresolved_conflicts_open_idx
    ON unresolved_conflicts (org_id, table_name, record_id)
    WHERE resolved_at IS NULL;

COMMENT ON TABLE unresolved_conflicts IS
    'Layer 2 conflict repair queue per ADR 0006. Populated when two concurrent '
    'edits to reference data have concurrent version vectors. Never auto-'
    'resolved — surfaced in the manager repair UI.';


-- -----------------------------------------------------------------------------
-- 5. Reference-data version-vector column (Layer 2)
-- -----------------------------------------------------------------------------
-- Added as an ALTER on the existing tables in Phase 3. Documented here so
-- Phase 1/2 code knows the shape. Version vector is JSONB of the form:
--   {"<branch_uuid>": <counter_int>, ...}
-- Bumping = increment the writing branch's own counter; comparison follows
-- standard vector-clock rules (dominates / dominated / concurrent).
-- -----------------------------------------------------------------------------

-- ALTER TABLE customers        ADD COLUMN version_vector JSONB NOT NULL DEFAULT '{}';
-- ALTER TABLE drugs            ADD COLUMN version_vector JSONB NOT NULL DEFAULT '{}';
-- ALTER TABLE drug_categories  ADD COLUMN version_vector JSONB NOT NULL DEFAULT '{}';
-- ALTER TABLE price_contracts  ADD COLUMN version_vector JSONB NOT NULL DEFAULT '{}';
-- ALTER TABLE branch_settings  ADD COLUMN version_vector JSONB NOT NULL DEFAULT '{}';
