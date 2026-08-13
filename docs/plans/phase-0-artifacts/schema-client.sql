-- =============================================================================
-- Phase 0 artifact: client-side SQLite schema for the event-sourced sync spine
-- =============================================================================
-- Canonical schema for the tables introduced on the Tauri client by ADRs 0006,
-- 0007. Phase 2 translates this into a `migrate_v23(db)` function in
-- ui.laso/src/lib/localDb.ts.
--
-- Related ADRs:
--   0006 — Event-Sourced Sync Spine
--   0007 — Event Schema, Hash Chain, and Dependency Semantics
--   0008 — Sync Push/Pull Endpoint Contracts
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. event_outbox — pending events awaiting server acceptance
-- -----------------------------------------------------------------------------
-- The client writes every mutation as an event to this table first, then
-- projects into local read tables, then ships to the server. FIFO by
-- outbox_id (which mirrors event_id insertion order via ULID).
--
-- One row per authored event. Never edited after write except to update
-- status/last_error/attempts on push cycles. Deleted from this table only
-- after the server has assigned a server_seq (see event_shipped_ack table).
-- -----------------------------------------------------------------------------

CREATE TABLE event_outbox (
    event_id           TEXT      NOT NULL PRIMARY KEY,     -- ULID, matches envelope
    aggregate_id       TEXT      NOT NULL,                 -- UUID string
    aggregate_type     TEXT      NOT NULL,
    event_type         TEXT      NOT NULL,
    schema_version     INTEGER   NOT NULL DEFAULT 1,
    payload_json       TEXT      NOT NULL,                 -- JSON (SQLite has no JSONB)
    dependencies_json  TEXT      NOT NULL DEFAULT '[]',    -- JSON array of ULIDs
    authored_at        TEXT      NOT NULL,                 -- ISO 8601
    authored_by        TEXT      NOT NULL,                 -- UUID string
    branch_id          TEXT      NOT NULL,
    org_id             TEXT      NOT NULL,
    hash_self          TEXT      NOT NULL,                 -- SHA-256 hex
    -- lifecycle
    status             TEXT      NOT NULL DEFAULT 'pending',  -- 'pending' | 'in_flight' | 'shipped'
    attempts           INTEGER   NOT NULL DEFAULT 0,
    last_attempt_at    TEXT,
    next_attempt_at    TEXT,
    last_error         TEXT,
    server_seq         INTEGER,                            -- populated on 'shipped'
    shipped_at         TEXT,

    CHECK (status IN ('pending','in_flight','shipped'))
);

-- FIFO drain — pending events ordered by ULID (== insertion order).
CREATE INDEX event_outbox_pending_idx
    ON event_outbox (status, event_id)
    WHERE status = 'pending';

-- Aggregate replay (local projection resume) — WHERE aggregate_id = ? ORDER BY event_id.
CREATE INDEX event_outbox_aggregate_idx
    ON event_outbox (aggregate_id, event_id);

-- Retry scheduling — WHERE status='pending' AND next_attempt_at <= now.
CREATE INDEX event_outbox_retry_schedule_idx
    ON event_outbox (next_attempt_at)
    WHERE status = 'pending';


-- -----------------------------------------------------------------------------
-- 2. event_dead_letter — client-side terminal failures (per ADR 0007)
-- -----------------------------------------------------------------------------
-- When the server returns 'rejected_permanent' for an event, the client moves
-- it from event_outbox to here. The failures panel reads from this table.
-- Manual user action ("void", "recreate") produces new events; the dead-letter
-- row is retained for the audit trail.
--
-- Not synced to the server (per ADR 0007 open items — default is client-only).
-- -----------------------------------------------------------------------------

CREATE TABLE event_dead_letter (
    event_id           TEXT      NOT NULL PRIMARY KEY,
    aggregate_id       TEXT      NOT NULL,
    aggregate_type     TEXT      NOT NULL,
    event_type         TEXT      NOT NULL,
    schema_version     INTEGER   NOT NULL,
    payload_json       TEXT      NOT NULL,
    dependencies_json  TEXT      NOT NULL,
    authored_at        TEXT      NOT NULL,
    authored_by        TEXT      NOT NULL,
    branch_id          TEXT      NOT NULL,
    org_id             TEXT      NOT NULL,
    hash_self          TEXT      NOT NULL,
    -- failure metadata
    server_error_code  TEXT      NOT NULL,
    server_error_msg   TEXT      NOT NULL,
    attempts_before_dl INTEGER   NOT NULL,
    dead_lettered_at   TEXT      NOT NULL,
    -- user action
    acknowledged_at    TEXT,                              -- user has seen it in the failures panel
    acknowledged_by    TEXT,
    resolution         TEXT,                              -- 'voided' | 'replaced' | 'ignored'
    resolution_event_id TEXT,                             -- FK-in-spirit to the compensating event

    CHECK (resolution IS NULL OR resolution IN ('voided','replaced','ignored'))
);

CREATE INDEX event_dead_letter_unresolved_idx
    ON event_dead_letter (dead_lettered_at)
    WHERE resolution IS NULL;

CREATE INDEX event_dead_letter_aggregate_idx
    ON event_dead_letter (aggregate_id);


-- -----------------------------------------------------------------------------
-- 3. sync_cursor — per-client pull cursor (replaces sync_meta's ad-hoc keys)
-- -----------------------------------------------------------------------------
-- One row per (org_id, aggregate_type) tracking the highest server_seq the
-- client has successfully projected locally. The next pull uses this as
-- after_seq (see ADR 0008).
--
-- Kept as a table rather than key-value in sync_meta so cursor advancement
-- is a single UPDATE with clear atomicity guarantees.
-- -----------------------------------------------------------------------------

CREATE TABLE sync_cursor (
    org_id           TEXT      NOT NULL,
    aggregate_type   TEXT      NOT NULL,
    last_seq         INTEGER   NOT NULL DEFAULT 0,
    last_updated_at  TEXT      NOT NULL,

    PRIMARY KEY (org_id, aggregate_type)
);


-- -----------------------------------------------------------------------------
-- 4. Deprecated tables (dropped in Phase 4)
-- -----------------------------------------------------------------------------
-- The following tables live in the current schema (up to migrate_v22) and
-- will be dropped in Phase 4 after Layer 1 fully replaces them:
--
--   sync_queue      -- legacy per-record push queue (sales)
--   crsql_changes   -- cr-sqlite internal (dropped with extension)
--   crsql_master    -- cr-sqlite internal
--   crsql_pack_columns  -- cr-sqlite internal
--   crsql_site_id   -- cr-sqlite internal
--   crsql_tracked_peers -- cr-sqlite internal
--
-- Read-model tables (customers, prescriptions, sales, ...) are RETAINED —
-- projectors write to them exactly as the current code does. Only the sync-
-- layer plumbing changes.
-- -----------------------------------------------------------------------------
