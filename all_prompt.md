You are working on the Pharmacare offline-first Tauri application.

Project structure:

* Frontend/Tauri app: `ui.laso`
* Local SQLite and CR-SQLite logic: `ui.laso/src/lib/localDb.ts`
* Sync engine: `ui.laso/src/lib/syncEngine.ts`
* Backend: `backend.laso`
* The desktop application must remain fully usable offline and synchronize safely when connectivity returns.

## Objective

Diagnose and permanently fix the CR-SQLite initialization and synchronization failure shown in the attached logs.

Do not apply a superficial workaround. Do not merely suppress the errors, remove tables from synchronization, delete the local database, or mark initialization as successful when CRR tracking is unavailable.

The work is complete only after existing databases and clean installations both migrate correctly, CRR tracking is verified, synchronization succeeds, and the complete test procedure passes repeatedly.

## Current failure

CR-SQLite is being loaded and `crsql_as_crr()` is being attempted, but these tables cannot be converted:

* `drug_batches`
* `customers`
* `prescriptions`
* `purchase_orders`
* `drug_categories`
* `price_contracts`
* `audit_logs`

The reported failures are:

1. These tables have no primary key, or CR-SQLite sees their primary key as nullable:

   * `drug_batches`
   * `customers`
   * `purchase_orders`
   * `drug_categories`
   * `price_contracts`
   * `audit_logs`

2. `prescriptions` has one or more unique indexes or unique constraints other than its primary key. CR-SQLite does not permit that on a CRR table.

3. `getDb()` reports a migration failure, but `loadPersistedQueueState()` and the sync engine continue running, producing unhandled promise rejections and repeated deterministic synchronization attempts.

Relevant error locations include approximately:

* `localDb.ts:50`
* `localDb.ts:1430`
* `localDb.ts:1443`
* `localDb.ts:1644`
* `syncEngine.ts:201`
* `syncEngine.ts:202`
* `syncEngine.ts:563`
* `syncEngine.ts:981`
* `syncEngine.ts:1087`

Treat line numbers as approximate because the file may change.

## Required investigation

Before editing anything, inspect the implementation and the real runtime schema.

Find:

* The SQLite database filename and exact path used by the Tauri application.
* Every migration that creates or modifies the seven affected tables.
* The current `CREATE TABLE` SQL for each affected table.
* All indexes, unique indexes, triggers and foreign keys attached to the tables.
* The list of tables passed to `crsql_as_crr()`.
* How migration versions are tracked.
* Whether migrations run before or after CRR initialization.
* How errors from Tauri SQL commands are represented.
* How automatic synchronization is scheduled and retried.
* How clean databases and existing user databases differ.

Run equivalent schema diagnostics against the actual application database:

```sql
SELECT name, type, sql
FROM sqlite_master
WHERE tbl_name IN (
    'drug_batches',
    'customers',
    'prescriptions',
    'purchase_orders',
    'drug_categories',
    'price_contracts',
    'audit_logs'
)
ORDER BY tbl_name, type, name;

PRAGMA table_info('drug_batches');
PRAGMA table_info('customers');
PRAGMA table_info('prescriptions');
PRAGMA table_info('purchase_orders');
PRAGMA table_info('drug_categories');
PRAGMA table_info('price_contracts');
PRAGMA table_info('audit_logs');

PRAGMA index_list('prescriptions');
PRAGMA foreign_key_check;
```

For every index returned for `prescriptions`, inspect it using:

```sql
PRAGMA index_info('INDEX_NAME');
```

Do not assume that the TypeScript table definitions match the schema already stored in an existing SQLite database.

## Required schema fix

Create a new forward-only, versioned migration that safely upgrades existing databases.

Do not edit only the original `CREATE TABLE IF NOT EXISTS` statements, because that will not repair tables already created on users’ machines.

### Primary-key requirements

Every replicated table must have an explicitly declared, non-null primary key.

The final table declarations must contain an acceptable primary key such as:

```sql
id TEXT PRIMARY KEY NOT NULL
```

Preserve the existing identifier type and semantics where possible. Do not convert identifiers or regenerate existing IDs unless the existing data proves that it is necessary.

Before rebuilding a table, validate that:

* Every existing row has a primary-key value.
* There are no duplicate primary-key values.
* The selected primary-key column is genuinely the record identifier.
* Foreign-key relationships still reference the correct identifiers.

If invalid rows exist, fail the migration with a precise diagnostic instead of silently deleting or rewriting data.

### Rebuilding SQLite tables

Because SQLite cannot reliably alter an existing primary-key/nullability declaration in place, rebuild each affected table safely.

For each table:

1. Read and preserve its complete current schema.
2. Create a replacement table with the corrected primary key.
3. Preserve all columns, data types, defaults, checks and foreign keys.
4. Copy all existing rows explicitly by column name.
5. Verify source and destination row counts.
6. Drop the old table.
7. Rename the replacement table.
8. Recreate valid non-unique indexes and required triggers.
9. Run foreign-key and integrity checks.
10. Roll back the entire migration if any step fails.

Use an atomic transaction where supported.

Handle `PRAGMA foreign_keys` correctly. Remember that changing `PRAGMA foreign_keys` inside an active transaction may not take effect. Structure the migration appropriately and always restore the intended foreign-key setting.

Do not use `SELECT *` when copying data.

Do not lose user data.

### `prescriptions` unique indexes

Find every unique constraint and unique index on `prescriptions`.

The final CRR table must not have any unique constraint or unique index apart from its primary key.

For each removed uniqueness rule:

* Determine the business reason for it.
* Preserve lookup performance with a normal non-unique index when appropriate.
* Move business uniqueness validation to the authoritative backend or another CR-SQLite-compatible design.
* Define deterministic behavior for records that arrive from multiple devices with the same business identifier.
* Do not silently weaken an important business rule without documenting how it is now enforced.

Do not remove the primary key.

## Required initialization order

Ensure startup follows this sequence:

1. Open the local SQLite database.
2. Load and verify the CR-SQLite extension.
3. Run all ordinary/versioned schema migrations.
4. Validate the final schema of every required CRR table.
5. Enable CRR tracking using `crsql_as_crr()`.
6. Verify that each required table is genuinely tracked.
7. Load persisted synchronization state.
8. Start pull/push synchronization.

The sync engine must not start if database initialization or required CRR verification fails.

## Required CRR verification

Do not trust metadata or a previous flag alone.

After calling `crsql_as_crr()`:

* Verify every required table using CR-SQLite’s actual metadata/change-tracking structures.
* Where practical, perform a transaction-safe probe proving that a change on the table is represented in `crsql_changes`.
* Roll back or clean up probe data.
* Report exactly which table failed and why.

Only mark the local database as sync-ready after every required table passes.

## Error handling requirements

Normalize unknown JavaScript/Tauri errors into real `Error` instances.

Do not throw or log raw objects that become:

```text
[object Object]
```

Create or reuse a safe error-normalization helper that extracts fields such as:

* `message`
* `code`
* `details`
* nested error information

Preserve the original cause where supported.

Fix `loadPersistedQueueState()` so that:

* Its rejection is always awaited or caught.
* It never starts after `getDb()` fails.
* It cannot produce an unhandled promise rejection.
* A corrupted queue-state record produces a clear recoverable or fatal error classification.

## Retry behavior

A schema incompatibility or missing required CRR tracking is a deterministic configuration/migration failure.

It must be classified as non-retryable until the application is restarted after repair.

When such a failure occurs:

* Stop the current synchronization attempt.
* Cancel or disable automatic retry timers.
* Set the UI sync state to a clear error state.
* Show a useful diagnostic without flooding the console.
* Do not repeatedly call `ensureCrrTablesEnabled()`.
* Do not advance pull or push cursors.
* Do not clear pending offline mutations.

Continue automatic retries only for genuinely transient failures such as:

* Temporary network loss
* Request timeout
* Temporary server unavailability
* Retryable HTTP responses

## Data safety requirements

Do not:

* Delete the SQLite database as the production fix.
* Clear tables to make the migration pass.
* Drop offline queue entries.
* Advance synchronization cursors after partial failure.
* Treat partially migrated schemas as successful.
* hide errors with empty `catch` blocks.
* disable CRR requirements for affected tables merely to make the application start.
* change production data without a migration and validation path.

Back up or copy the test database before destructive migration tests.

## Tests to add

Add automated tests where the project’s architecture permits them.

At minimum, cover:

### Clean installation

* Start with no local database.
* Run all migrations.
* Confirm all affected tables exist.
* Confirm every required table has an explicit non-null primary key.
* Confirm `prescriptions` has no additional unique indexes.
* Confirm every required table becomes a CRR.
* Confirm startup completes without unhandled errors.

### Upgrade from old schema

Create a fixture database representing the currently broken schema.

Populate every affected table with representative data and relationships.

Run the new migration and verify:

* No rows are lost.
* IDs are unchanged.
* Row counts match.
* Foreign keys remain valid.
* Required normal indexes are restored.
* Forbidden unique indexes are removed only where required.
* Every required table becomes a CRR.
* Running the migration again is safe or correctly recognized as already applied.

### Migration rollback

Force a failure midway through a table rebuild.

Verify:

* The database remains in its original consistent state.
* No replacement tables are left behind.
* Migration version is not advanced.
* The application does not start synchronization.

### Initialization failure

Simulate a CRR conversion failure.

Verify:

* `getDb()` rejects with a readable `Error`.
* Queue-state loading does not run.
* Synchronization does not start.
* No unhandled promise rejection occurs.
* Automatic retries stop for the fatal schema error.

### Transient sync failure

Simulate network failure after successful database initialization.

Verify:

* The failure is classified as retryable.
* Pending changes remain queued.
* Cursors do not advance incorrectly.
* A later retry can succeed.

### Pull and push

After a successful migration:

* Insert a local record into each writable synchronized table.
* Confirm CR-SQLite records the changes.
* Push the changes.
* Verify backend acceptance.
* Pull server changes into a second clean local database or test replica.
* Confirm records and updates converge correctly.
* Test updates and deletes, not only inserts.

## Manual end-to-end test

Run the real Tauri application, not only isolated unit tests.

Perform this sequence:

1. Back up the existing local SQLite database.
2. Start the application using a database containing the old broken schema.
3. Confirm the new migration runs once.
4. Confirm there are no migration, CRR or unhandled-promise errors.
5. Confirm the sync indicator leaves its loading state.
6. Confirm existing customers, batches, prescriptions, categories, purchase orders, contracts and audit data remain visible.
7. Disconnect the machine from the network.
8. Create a customer offline.
9. Complete an offline sale using locally available inventory and customer data.
10. Perform other supported offline writes involving affected tables.
11. Restart the application while still offline.
12. Confirm offline data and pending operations survive restart.
13. Restore connectivity.
14. Trigger or wait for synchronization.
15. Confirm pending changes are pushed exactly once.
16. Confirm server changes are pulled.
17. Confirm there are no duplicate records.
18. Confirm inventory quantities and sales totals remain correct.
19. Restart again and confirm synchronization does not repeat completed operations.
20. Inspect the console and backend logs for hidden errors.

Repeat the offline → restart → reconnect → synchronize cycle at least twice.

## Regression checks

Also verify that the fix does not break:

* Existing POS workflows
* Customer lookup offline
* Batch and stock selection
* Prescription handling
* Purchase-order handling
* Price contracts
* Audit-log display
* App startup with and without internet
* Existing CRR-enabled tables
* Sync cursor persistence
* Conflict resolution
* Production build
* Linux x86_64 development
* ARM64 production packaging or extension selection, where applicable

Run the project’s relevant commands, including available equivalents of:

```bash
npm run typecheck
npm run lint
npm test
cargo check
cargo test
```

Use the actual package manager and scripts defined in the repository.

Build the Tauri application or run the closest production-mode build available.

## Iteration rule

Do not stop after making the first code change.

Use this loop:

1. Reproduce the failure.
2. Identify the direct and underlying causes.
3. Implement the smallest complete architectural fix.
4. Run focused tests.
5. Run the full relevant test suite.
6. Run the real application.
7. Inspect frontend, Tauri and backend logs.
8. Fix every newly discovered related problem.
9. Repeat until all acceptance criteria pass.

Do not report success based only on compilation or the absence of the original console message.

## Acceptance criteria

The task is complete only when all of the following are true:

* Existing broken databases migrate without data loss.
* Clean databases initialize correctly.
* All required CRR tables have valid explicit non-null primary keys.
* `prescriptions` has no forbidden additional unique index.
* Every required table is verified as CRR-enabled.
* Queue-state loading starts only after successful database initialization.
* No unhandled promise rejection occurs.
* Fatal schema failures do not enter an infinite retry loop.
* Transient network failures still retry correctly.
* Offline records survive application restart.
* Push and pull synchronization both succeed.
* Cursors advance only after confirmed successful processing.
* No duplicate records are created.
* Full tests, type checks, Rust checks and production build pass.
* The real offline/reconnect workflow passes at least twice.
* Logs remain free of the original failures and any new unexplained errors.

## Final report format

When finished, provide:

1. Root cause
2. Files changed
3. Migration design
4. Exact schema changes per table
5. Treatment of the `prescriptions` uniqueness rule
6. Initialization and error-handling changes
7. Retry-behavior changes
8. Automated tests added
9. Commands executed and their outputs
10. Manual end-to-end steps performed
11. Before-and-after schema evidence
12. CRR verification evidence for every required table
13. Data-preservation evidence
14. Remaining risks or limitations
15. Final git diff summary

Do not claim completion if any acceptance criterion is untested or failing. Clearly state anything that could not be verified and why.
