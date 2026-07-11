# Sync Fix Plan — CRR Offline Sales & Audit Logs

## Objective
Fix offline sync for sales and customers created while disconnected (CRR path is unreliable).
Make audit logs available offline after server is reached.

## Phase 1a — CRR Table Tracking Fix

### Problem
`CRR_TABLES` is a hardcoded static `Set` (`localDb.ts:1113`). When `crsql_as_crr()` fails in migration (`localDb.ts:972`, silently swallowed), the table is still in the set so `enqueue()` returns early — the row is orphaned in both sync_queue and cr-sqlite.

### Changes

**`ui.laso/src/lib/localDb.ts`**

1. **Migration `migrateToV16()` (~line 962-974):**
   - After each `crsql_as_crr()` success, write `crr_enabled_<table> = "1"` to `sync_meta`.
   - On failure, write `crr_enabled_<table> = "0"` instead.

2. **Replace static `CRR_TABLES` set (line 1113-1120):**
   - Remove `export const CRR_TABLES = new Set([...])`.
   - Add:
   ```ts
   let _crrCache: Record<string, boolean> | null = null;

   export async function isCrrTable(table: string): Promise<boolean> {
     if (_crrCache === null) {
       const db = await getDb();
       const rows = await db.select<{ key: string; value: string }[]>(
         "SELECT key, value FROM sync_meta WHERE key LIKE 'crr_enabled_%'"
       );
       _crrCache = {};
       for (const row of rows) {
         _crrCache[row.key.replace("crr_enabled_", "")] = row.value === "1";
       }
     }
     return _crrCache[table] ?? false;
   }

   export function resetCrrCache(): void {
     _crrCache = null;
   }
   ```

3. **`enqueue()` function (line 1181-1218):**
   - Change line 1189 from `if (CRR_TABLES.has(tableName)) return;` to `if (await isCrrTable(tableName)) return;`.

**`ui.laso/src/lib/syncEngine.ts`**

4. **Update import (line 32):**
   - Change `CRR_TABLES,` to `isCrrTable,`.

5. **Six guard clauses in `applyPullResponse()` (lines 691, 702, 721, 729, 738, 770):**
   - Each `!CRR_TABLES.has("table")` → `!(await isCrrTable("table"))`.

## Phase 1b — pushCrr Retry

### Problem
`pushCrr()` (line 450) catches all errors silently. On failure, changes are lost.

### Changes

**`ui.laso/src/lib/syncEngine.ts`**

1. **Wrap `pushCrr()` inner loop (lines 465-498) in retry block:**
   - Exponential backoff, max 3 attempts.
   - On persistent failure, for each failed change call `enqueue(table, localId, "create", 1, payload)` as fallback to sync_queue.
   - Report failures through `this.logError()`.

## Phase 1c — Wire offlineSalesManager

### Problem
`offlineSalesManager.getPendingSales()` / `getSalesReadyForRetry()` are dead code — zero callers.

### Changes

**`ui.laso/src/lib/syncEngine.ts`**

1. **In `sync()` method (after `pushCrr()` call at line 211):**
   - Import and call `offlineSalesManager.getPendingSales()`.
   - For each pending sale with `sync_status !== "synced"`, call `enqueue("sales", sale.id, "create", 1, sale.sale_data)` to re-queue it through the (now working) CRR or sync_queue path.

## Phase 2 — Audit Logs Offline

### Problem
`AuditLog` model lacks `SyncTrackingMixin`, no sync endpoint exists, local DB has no `audit_logs` table.

### Changes

**Backend `backend.laso/app/models/system_md/sys_models.py`:**
- Add `SyncTrackingMixin` to `AuditLog`.

**Backend sync endpoint:**
- Add audit_logs to pull response schema.
- New query in sync pull handler for audit_logs.

**Client `ui.laso/src/lib/localDb.ts`:**
- Add `audit_logs` table schema.

**Client `ui.laso/src/lib/syncEngine.ts`:**
- Pull audit_logs like other pull-only tables.

**UI `AuditLogPage.tsx`:**
- Fall back to local cache when offline.

## Root Causes (Summary)

| Issue | Root Cause | File:Line |
|---|---|---|
| Sales/customers not syncing | `CRR_TABLES` static set; crsql_as_crr() failure swallowed; enqueue() returns early | `localDb.ts:1113`, `:967`, `:1189` |
| ORPHANED rows never retried | `offlineSalesManager.getPendingSales()` has zero callers | `offlineSalesManager.ts:326` |
| pushCrr has no retry | catch block only console.warns | `syncEngine.ts:499-501` |
| Audit logs not offline | No SyncTrackingMixin, no sync endpoint, no local table | `sys_models.py:19`, `syncEngine.ts:46` |
