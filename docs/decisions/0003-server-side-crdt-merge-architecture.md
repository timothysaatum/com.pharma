# 0003: Server-Side CRDT Merge Architecture

**Status:** Superseded by [0006](0006-event-sourced-sync-spine.md) (2026-08-12)
**Implemented and retired:** 2026-08-14 — `shadow_db.py`, `crr_sync_service.py`, `crr_sync_endpoints.py` deleted server-side; cr-sqlite extension made optional and `crsql_*` tables dropped client-side via localDb v26 migration.
**Date:** 2026-07-10

## Context
cr-sqlite (vlcn-io) is a SQLite-only extension. There is no Postgres-native equivalent —
Postgres cannot participate in the crsql_changes / col_version / site_id CRDT protocol
directly. The FastAPI server needs a way to receive, merge, and durably store changes
from multiple offline Tauri clients while keeping Postgres as the source of truth for
everything outside the sync path (reports, admin tools, other services).

## Decision
The server runs its own SQLite file with cr-sqlite loaded, acting as one additional peer
("site") in the CRDT mesh — not a passive relay.

Flow:
1. Client POSTs its crsql_changes rows to /sync/crr-push
2. Server validates (org scope, FK checks, field whitelist) — same rules as today
3. Server INSERTs validated rows into its own crsql_changes table
4. cr-sqlite's triggers auto-merge into the server's shadow SQLite tables
5. Server upserts the affected row's merged state into real Postgres
   (plain SQL, ON CONFLICT DO UPDATE) — Postgres never runs cr-sqlite
6. GET /sync/pull returns the server's own crsql_changes (as a site) since
   the client's last synced version

## Rationale
- Keeps Postgres clean and queryable by non-sync systems without any CRDT awareness
- Reuses the exact merge logic already verified in the cr-sqlite spike — no new
  conflict-resolution code to write
- Avoids adopting a different product (e.g. sqliteai/sqlite-sync, which syncs CRDTs
  natively to Postgres) that would require re-spiking from scratch and carries a
  commercial license requirement for production use

## Consequences
- Server needs a persistent SQLite file (shadow DB) alongside Postgres — plan storage,
  backup, and disk considerations for this file, same as any stateful server component
- Every merge requires a two-step write: shadow SQLite merge, then Postgres upsert.
  This upsert step needs its own error handling — if it fails after the SQLite merge
  succeeds, the two stores can drift, so wrap both in a way that allows retry/reconciliation
- Postgres schema does NOT need cr-sqlite's DEFAULT-value constraints — only the local
  SQLite (client + server shadow) schemas do

## Trigger for Revisit
Revisit if the shadow-SQLite + upsert step becomes an operational burden (e.g. drift
between shadow SQLite and Postgres becomes a recurring incident) — at that point,
evaluate a native Postgres CRDT sync product instead of maintaining this bridge.
