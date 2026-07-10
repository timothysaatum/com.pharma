# 0002: Local DB Concurrency Model

**Status:** Accepted  
**Date:** 2026-07-10  
**Context:** Migrating from `tauri-plugin-sql` (managed its own connection) to `rusqlite` with a single `Mutex<Connection>` shared across all Tauri commands.

## Decision

Stay on `Mutex<Connection>` — do **not** introduce r2d2 connection pooling now.

## Rationale

A full audit of concurrent access patterns found the worst realistic load is a background sync cycle (dozens of sequential row-by-row writes over 5–30 s) interleaved with a handful of UI-triggered reads (debounced search, mount effects). Even at peak, this produces **~1–3 DB ops/s** — well below the Mutex contention threshold (spike showed ~3 ms/write, ~5 µs/read). The sync cycle's visible latency comes from JS processing per row, not SQLite locking.

Adding r2d2 means every pooled connection must load the cr-sqlite extension separately, adding complexity. The single `Mutex<Connection>` is correct for current load.

## Trigger for Revisit

Introduce r2d2 only if **UI-perceived lag during background sync** is observed and profiled to Mutex contention. The upgrade path: wrap connections in `r2d2::Pool<ConnectionManager>` with `load_extension` called after checkout, then dispatch read commands to a separate pool handle so reads can bypass a busy writer.
