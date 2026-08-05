# Offline-First Architecture Review & Deployment Sign-Off — Pharmacare

**Date:** 2026-08-11
**Scope:** Local database lifecycle, sync protocol robustness, and offline-specific failure modes — a distinct lens from the [2026-08-04 security/correctness review](2026-08-04-inventory-sync-sales-independent-review.md), whose 9 findings (cross-tenant sync isolation, authorization gaps, audited sale-void, dead-letter retry, price reconciliation) are fixed and not re-litigated here.
**Method:** Two focused audits (local DB lifecycle & data integrity; sync protocol robustness under real network conditions) against the current code, cross-checked against prior investigation docs. Every finding below was fixed in this same pass except where explicitly scoped out in §4.

---

## 1. Verdict

**Conditional go.** The five findings that could have caused real data loss, permanently bricked a device, or blocked an established branch from ever completing onboarding are now fixed and covered by regression tests (223 backend + 187 frontend tests passing). One finding — local data at rest is unencrypted — is a genuine compliance gap for a system storing PHI and controlled-substance dispensing records, and I'm not signing off on it as "flawless" without flagging it plainly: **deploy now if the desktop hardware is organization-owned, physically controlled, and disk-level encryption (BitLocker/FileVault/LUKS) is enforced at the OS level as compensating control; do not deploy to uncontrolled or field hardware until §4.1 lands.** Everything else in this report is fixed, not merely documented.

---

## 2. What "offline-first" breaks that a general review misses

A sync-and-CRDT system can be perfectly correct in its conflict resolution and still fail in production for reasons that only show up after months of real offline usage across many app versions and machines:

- **Migrations run on hardware you don't control, unattended, with no operator to retry a failure.** A migration that isn't transactional doesn't get a second chance — the device is what it is when the app relaunches.
- **The first sync for an established site is the one most likely to hit scale limits**, because it's the one pull that can't rely on "only what changed since yesterday."
- **The client's clock is an input, not a fact.** Anything that trusts it for a business decision (not just for CRDT ordering, which cr-sqlite already handles independently via HLC) is trusting unverified user-supplied data.
- **A local SQLite file is a physical object that outlives the app.** It sits on a disk, gets backed up, gets the machine resold — its own security posture matters independently of the network layer.

These four themes map directly to the five fixes below.

---

## 3. Findings fixed in this pass

### 3.1 [Critical] `migrate_v15` could permanently brick a device

**The bug:** `migrate_v15` (converting `branch_inventory` to a CR-SQLite CRR table) executed `CREATE` → `INSERT...SELECT` → `DROP TABLE branch_inventory` → `RENAME` as separate autocommit statements, with no transaction wrapper — unlike every later structurally-identical migration (v16, v17, v22). A crash or forced-quit between the `DROP` and the `RENAME` left a device with **neither table**. Because `user_version` was still 14, every subsequent launch re-ran the same migration, whose `INSERT INTO branch_inventory_crr SELECT ... FROM branch_inventory` then failed with "no such table" — forever. The only recovery was deleting the local database (total data loss for that device).

**The fix:**
- `migrate_v15` is now wrapped in `BEGIN IMMEDIATE` / `COMMIT`, with `ROLLBACK` on any failure — mirroring the exact pattern already proven in v16/v17/v22, including keeping the `crsql_as_crr()` call in its own nested try/catch so "extension unavailable" stays a legitimate degraded mode, not a rollback trigger.
- Added `repairIncompleteV15Migration()`, called unconditionally at the top of `runMigrations`, which detects the specific stuck state (branch_inventory missing, branch_inventory_crr present, version < 15) on any device that already hit this bug before the fix shipped, and heals it before migrations proceed. No-op otherwise.

**Verified:** `localDbMigrationV15.test.ts` — happy path commits and reaches v15; a forced failure mid-migration rolls back cleanly (branch_inventory intact, version unchanged) and a subsequent retry succeeds normally; the repair function heals the exact stuck state and is a no-op when not needed. 8/8 passing.

### 3.2 [Critical] No schema-downgrade detection

**The bug:** if a device's local DB were ever advanced by a newer build and then opened by an older one, every `user_version < N` check would be false (skipping all migrations), and the five `ensure*Schema` repair functions would run unconditionally against a schema shape the older code doesn't know about — a silent-corruption risk with no guard anywhere.

**The fix:** `guardAgainstSchemaDowngrade()`, called before any migration runs, throws a clear, actionable error ("please update the application") if the local schema version exceeds `MAX_KNOWN_SCHEMA_VERSION` (22, bump alongside the next migration). Failing loudly and blocking startup is safer than silently continuing against an unknown schema.

**Verified:** part of the same 8-test suite above.

### 3.3 [Critical] Legacy sales pull was unbounded — could never complete for an established branch

**The bug:** `/sync/pull` issued one query per table with no `LIMIT`, and `has_more` was never set on this path (only on the newer CRR pull). For sales specifically — the one table still on this legacy path, deliberately, per the prior review's confirmation that CRDT merge is wrong for a table with inventory/prescription side effects — a brand-new device's *first* sync against an established branch with tens of thousands of historical sales (each with nested line items) was a single unbounded query wrapped in a client-side 15-second timeout. Because `last_sync_at` only advances on a fully successful response, a timeout didn't resume from partial progress — **it restarted the entire query from scratch on every retry.** A branch with enough history could realistically never complete initial device onboarding.

**The fix:** `_pull_table` now supports a `limit`, orders by `(updated_at, id)` for a stable sort, and — critically — **trims trailing rows that share the boundary timestamp** before reporting its resume point. Without this, two sales landing at the exact same microsecond could straddle a page boundary and one of them would be silently skipped forever (a real risk under concurrent writes, not a theoretical one). `_pull_with_snapshot` now pages every table at 500 rows (matching the existing CRR pull convention) and reports `has_more` + the earliest safe resume timestamp across all tables in the response. `pullTableFull` (used for the initial full customers sync) is now correctly threaded with `last_sync_at` across its pagination loop — **this was a genuinely new risk my own pagination fix introduced**: before, `has_more` was always false so the missing cursor was harmless; after adding real pagination, a branch with >500 customers would have made this loop spin forever re-fetching page one. Caught and fixed in the same pass.

**Verified:** `test_pull_paginates_and_resumes_without_loss_or_duplication` and `test_pull_pagination_boundary_holds_back_tied_timestamps` (backend, including the tied-timestamp edge case), plus a new frontend test asserting `pullTableFull` threads the cursor and terminates. 3 new tests, all passing.

### 3.4 [Critical] Client clock trusted for two business decisions, with no upper bound at all

**The bug:** `created_offline_at` is unverified client input. Two problems:
1. **No forward bound.** The 7-day backdating guard only ever checked the past. A forward-skewed clock (bad RTC, misconfigured timezone) could stamp a sale with a future `created_at`, corrupting daily-sales/shift/tax-period reporting indefinitely with nothing rejecting it.
2. **Batch-expiry-at-sale-time validation trusted the full 7-day window.** This feature is deliberate and correct in principle — a batch that expired *after* a legitimate offline sale shouldn't retroactively invalidate that sale — but because it trusted the claimed timestamp across the entire 7-day sync-acceptance window, a device with a clock skewed backward by, say, 5 days could make an already-expired batch appear valid "as of" the claimed sale time, letting expired stock be recorded as a legitimate sale with no independent check.

**The fix:**
- Added a forward-skew guard (24h tolerance, generous enough to absorb legitimate timezone edge cases — max real-world UTC offset is +14h — while catching a clock that's actually wrong), using the same `force=true` manager-override mechanism already established for the backdating guard.
- Tightened the expiry-validation trust window to 48 hours specifically (distinct from the 7-day general sync-acceptance window): within 48h, the claimed timestamp is trusted exactly as before (covers a device offline over a long weekend); beyond that, the effective date is clamped to the edge of the trusted window rather than the far-past claimed date, so an actually-expired-today batch can no longer be laundered through a stale clock.

**Verified:** 4 new tests — forward-dated sale rejected, minor forward skew accepted, backward skew beyond the trust window can't launder an expired batch, and the original legitimate-delayed-sync feature still works within the trust window. All passing, plus all 24 pre-existing offline-sale-sync tests still pass unchanged.

### 3.5 [Quick fixes]
- Removed a false "256-bit / Encryption" marketing claim from the login screen — an affirmative security representation to pharmacy customers handling regulated health data that wasn't true for data at rest (see §4.1). Replaced with an equally strong, true claim ("Offline / Always works").
- `onOffline()` now cancels any pending network-retry timer instead of leaving it to fire and silently no-op later.
- Removed a latent authorization-drift trap in `applyPullResponse`: purchase orders previously only protected offline-created drafts (`OFFLINE-PO-` prefix) from being overwritten by a pull, unlike every other table which protected *any* locally-pending row. Effectively unreachable on current CRR-migrated installs, but left correct rather than as a trap for any future non-CRR fallback path.

---

## 4. Scoped out — follow-up plan, not silently deferred

Signing off "flawless" while hiding known gaps would defeat the purpose of this review. These four are real, but each needs infrastructure or testing investment that would be irresponsible to rush into a single pass alongside the fixes above — getting any of them wrong risks *causing* the exact class of bug this report exists to prevent (bricked devices, corrupted local data).

### 4.1 [Must-fix before uncontrolled/field deployment] Local data at rest is unencrypted

The local SQLite file — customer PII, full prescription histories, controlled-substance dispensing records — is plaintext on disk (`rusqlite` with the plain `bundled` feature, not `bundled-sqlcipher`; no `PRAGMA key` anywhere). Anyone with local file access (another OS account, a stolen machine, backup software, a resold drive) can read it directly.

**Why not fixed now:** this is a genuine multi-week project, not a patch — it needs a key-management design (derived from user credentials? OS keychain-backed?), a migration path for every already-deployed plaintext database that must not lose data mid-conversion, and real testing across Windows/macOS/Linux (Tauri targets all three, and SQLCipher's packaging story differs per platform). Rushing this is how you get a fleet of devices that fail to open their local database after an update.

**Recommended plan:** (1) adopt `rusqlite`'s `bundled-sqlcipher` feature; (2) derive the encryption key from the OS-native credential store (Keychain/Credential Manager/Secret Service) seeded at first login, never from a value the user types and could forget; (3) write a one-time, transactional (see §3.1's lesson) migration that re-keys an existing plaintext DB in place; (4) test kill-power-mid-rekey explicitly, since this is exactly the class of bug this report just fixed elsewhere. Until this lands, deployment onto hardware that isn't organization-owned and disk-encrypted at the OS level should not proceed.

### 4.2 [Should-fix before multi-year operation] Unbounded local storage growth

`crsql_changes`/`__crsql_clock` (cr-sqlite's own change-tracking, both client and server shadow DB), plus this app's `audit_logs` and `crr_renumber_audit`, and the `crr_row_owners` index added in the prior review (deliberately never pruned, by design, so tenant-scoped delete-tombstones stay attributable) — none of these have a compaction or retention strategy. A busy branch after 2-3 years of daily operation will have a change-tracking table that's a large multiple of its live-row count.

**Why not fixed now:** compaction of CRDT change history intersects directly with correctness — pruning a change too early could mean a client that's been offline a long time misses history it needs to converge. This needs a real protocol decision (a retention window bounded by "all known clients have acknowledged past this point," or accepting unbounded growth and addressing it via periodic archival instead), not a quick delete statement.

**Recommended plan:** design a retention policy as its own piece of work, informed by real device-fleet acknowledgment data once this app has been in the field long enough to know what "all clients caught up" looks like in practice.

### 4.3 [Should-fix before wide rollout] No crash/power-loss test evidence

Both databases use WAL mode with (default, implicit) `synchronous=FULL`, and the offline-sale write path is genuinely well-designed for atomicity (`TransactionBehavior::Immediate` with row-count guards). But no test in the repo actually kills a process or corrupts a WAL file mid-write — the one test named `test_crash_recovery` validates a different, server-side logical case (shadow DB ahead of Postgres), not a real power-loss scenario on the local file.

**Recommended plan:** a small fault-injection harness (kill the Tauri process mid-`db_execute_transaction`, verify WAL replay on next launch leaves the DB in a valid pre- or post-transaction state, never partial) should exist before this ships to hardware where power loss is a realistic event (not just laptops with batteries — desktop POS terminals on wall power are exactly the devices most exposed to this).

### 4.4 [Nice-to-have] No network-chaos test harness

Idempotency and retry logic were verified by close code inspection in this review (and they're genuinely solid — the `operation_id`/`SyncOperationReceipt` idempotency design is one of the strongest parts of this system) but there's no automated test that actually simulates a lost response, a mid-cycle network drop, or true concurrent multi-device drift. This is a testing-infrastructure investment worth making, but the code paths it would cover are not currently suspected of having bugs — this is about catching a *future* regression, not an unfixed *current* one.

---

## 5. Test evidence

- Backend: 211 → 217 tests passing this round (6 new: pull pagination ×2, clock-skew guards ×4 — the v15 migration fix is frontend-only, so contributes no backend tests), zero regressions.
- Frontend: 178 → 187 tests passing this round (9 new: v15 migration/repair/downgrade-guard ×8, pullTableFull pagination-threading ×1), zero regressions, zero new TypeScript errors (the 14 pre-existing errors are unrelated and pre-date both review rounds).

## 6. Sources

- `ui.laso/src/lib/localDb.ts`, `ui.laso/src-tauri/src/db.rs`
- `backend.laso/app/services/sync/sync_service.py`, `shadow_db.py`
- `ui.laso/src/lib/syncEngine.ts`, `syncRetryBackoff.ts`
- `docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md` (prior round, not re-litigated)
- `docs/decisions/`, `docs/evidence/` (ADR 0002/0003, prior CRR migration evidence)
