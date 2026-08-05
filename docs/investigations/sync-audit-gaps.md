# Offline-Sync Audit — Consolidated Gaps

| # | Gap | Area | Severity |
|---|---|---|---|
| 1 | No service worker / background sync — only runs while app is open | Engine | Medium |
| 2 | No WebSocket/push notification for immediate sync — 30s polling interval | Engine | Low |
| 3 | No tombstone column on local tables — offline-deleted records stay in local DB until next pull | Schema | Low |
| 4 | Customer `manual_required` conflicts block queue indefinitely with no auto-resolve | Conflict | Medium |
| 5 | Sales push has no sync_version conflict check — relies solely on ID + sale_number uniqueness | Server | Low (by design) |
| 6 | `source_organization_id` in SyncTrackingMixin is populated but never queried in sync | Server | Low |
| 7 | High-concurrency false conflicts on same record from two clients (optimistic locking limitation) | Conflict | Low |
| 8 | No field-level merge — all-or-nothing per record | Conflict | Medium (feature gap) |
| 9 | Push endpoint has no max-batch-size enforcement beyond implicit HTTP body limits | Server | Low |
| 10 | Dead-lettered records require user to re-edit — no admin dashboard for resolution | Engine | Medium |
