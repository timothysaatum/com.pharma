# Sync Contract Foreign Key Debug Guide

## Problem Summary

When syncing sales with `price_contract_id`, you see:
```
sqlalchemy.exc.IntegrityError: insert or update on table "sales" 
violates foreign key constraint "sales_price_contract_id_fkey"
```

**Root Cause:** Your local Tauri `laso.sqlite3` contains old price contracts from a **different organization** or **previous sync session**. When the cashier selects a contract from the old data, the sale sync fails because that contract doesn't exist in the current organization's production database.

---

## Diagnosis

### 1. Check Local Cache Organization Mismatch

On your **local machine** (in Tauri app console):

```javascript
// Open DevTools → Console
const db = await getDb();
const oldContracts = await db.all("SELECT id, display, type FROM price_contracts LIMIT 10");
console.log("Local contracts:", oldContracts);

// Check current organization
const currentOrg = await fetch('/api/v1/auth/me').then(r => r.json());
console.log("Current org:", currentOrg.organization_id);
```

If the contract IDs in `laso.sqlite3` **don't exist** in your production database for the current org, they're stale.

### 2. Check Backend Sale Sync Logs

```bash
# On your server, watch live sync errors:
tail -f /var/log/pharma-backend.log | grep -i "foreign key\|price_contract"
```

Look for patterns like:
```
Sync: price_contract <UUID> for sale <UUID> not in org <ORG_ID>; clearing price_contract_id
Sync: FK constraint violation after validation for sale <UUID>; attempting recovery
```

### 3. Query Production Database

```sql
-- On production PostgreSQL, check if the contract exists
SELECT id, contract_code, contract_name, organization_id 
FROM price_contracts 
WHERE id = '<the-uuid-from-error>';

-- List all contracts for your organization
SELECT id, contract_code, contract_name, type 
FROM price_contracts 
WHERE organization_id = '<your-org-id>' 
AND is_deleted = false 
AND status = 'active';
```

---

## Solutions

### Solution 1: Clear Local Cache (Recommended)

**Fastest recovery** — delete the local SQLite cache so it re-syncs fresh data:

#### On Windows/Mac (Tauri app):
```javascript
// In DevTools console
const localDb = require('@/lib/localDb');
await localDb.clearAllTables();
console.log("Local cache cleared. Refresh the app to re-sync.");
```

Or manually:
- **Windows:** `%APPDATA%\Laso\laso.sqlite3` → Delete file
- **macOS:** `~/Library/Application Support/Laso/laso.sqlite3` → Delete file
- **Linux:** `~/.config/Laso/laso.sqlite3` → Delete file

Then restart Tauri app. It will automatically re-sync all data from the server.

#### On Backend (if you need to reset):
```bash
cd /path/to/backend
# (Only if running locally for testing)
rm laso.sqlite3
# Backend will create a fresh one on next sync push
```

---

### Solution 2: Trigger Bulk FK Cleanup on Server

If you can't afford to re-sync the entire client cache:

```bash
# Make authenticated request (replace with your token)
curl -X POST http://api.example.com/api/v1/admin/sync-recovery/bulk-fix \
  -H "Authorization: Bearer <your-jwt-token>" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```

This endpoint:
1. Finds all pending sales with invalid `price_contract_id`
2. Clears the contract IDs
3. Marks sales as synced with fixes applied

Response example:
```json
{
  "total_issues_found": 5,
  "total_fixed": 5,
  "fixed_by_type": {
    "missing_price_contract": 3,
    "stale_pending_sale": 2
  },
  "applied_fixes": [
    {
      "sale_id": "<uuid>",
      "issue_type": "missing_price_contract",
      "action": "cleared price_contract_id"
    }
  ]
}
```

---

### Solution 3: Prevent Future Mismatches

#### A. Sync Contract Whitelist (Client)
Before accepting a contract selection, validate it exists:

**In `ui.laso/src/pages/POSPage.tsx`:**
```typescript
const handleCheckout = async () => {
  if (!state.contract) {
    setError("No contract selected");
    return;
  }

  // Validate contract exists in current branch context
  const existsOnServer = await contractApi.getContract(state.contract.id)
    .catch(() => null);
  
  if (!existsOnServer) {
    alert("Selected contract no longer exists. Contracts may have changed.");
    // Clear cart and reset contract
    dispatch({ type: "CLEAR_CART" });
    setContracts([]); // Force reload
    return;
  }

  // Proceed with sale
  await processSale();
};
```

#### B. Organization Scoping
Ensure contracts are **always** filtered by organization:

**In `backend.laso/app/api/v1/endpoints/price_contract_endpoints.py`:**
```python
# Already done — but verify:
contracts = await PriceContractService.get_contracts(
    db, 
    current_user.organization_id,  # ← CRITICAL
    filters
)
```

#### C. Sync Timestamp Validation
The sync engine already validates contract existence in the current org during push (line 276–277 of `sync_service.py`), but ensure you're **always** pulling fresh contracts before syncing:

**In Tauri app sync flow:**
```typescript
async function syncContracts() {
  // 1. Pull fresh contracts from server
  const { price_contracts } = await syncApi.pull();
  
  // 2. Update local cache
  await cachePriceContracts(price_contracts);
  
  // 3. Only then push sales with contracts
  await syncApi.push(pendingSales);
}
```

---

## Verification

### Test That Fix Works

1. **Clear local cache** (Solution 1)
2. **Reload Tauri app**
3. **Open POS** → Search for Metronidazole
   - Should see **"Rx" badge** (now that `requires_prescription` is included)
4. **Add to cart** → Open contracts dropdown
   - Should see only **valid contracts** for your organization
5. **Complete sale** → Should sync successfully to server

### Monitor Sync Health

```bash
# Check sync status overview
curl -X GET http://api.example.com/api/v1/admin/sync-recovery/health \
  -H "Authorization: Bearer <token>"

# Response should show:
# - "total_sales": N
# - "sync_status_distribution": { "synced": M, "pending": 0, ... }
# - "stale_pending_count": 0  ← Should be zero
```

---

## Why This Happens

| Component | Issue |
|-----------|-------|
| **Local Tauri cache** | Contains old contracts from previous org/sync session; not organization-scoped on cache key |
| **Frontend contract dropdown** | Displays whatever is in local cache without org validation |
| **Cashier selects old contract** | Sale is created with `price_contract_id` pointing to non-existent contract |
| **Sync push fails** | Server FK validation detects contract doesn't exist in current org |
| **Backend recovery kicks in** | Clears contract ID; sale syncs without discount (acceptable fallback) |

---

## Prevention Checklist

- [ ] Local cache is cleared after **organization/branch switch**
- [ ] Contract dropdown only shows **active, current-org contracts**
- [ ] Contracts are **re-fetched** before each checkout session
- [ ] Sync logs are **monitored** for FK constraint warnings
- [ ] Manual validation of contract ID before **any large batch sync**

---

## When to Escalate

If after clearing cache + restarting the app, sync **still fails**:

1. **Collect debug info:**
   ```bash
   # Server logs (last 50 lines mentioning FK/contract)
   tail -50 /var/log/pharma-backend.log | grep -i "foreign\|contract"
   
   # Check PostgreSQL directly
   psql -U <db_user> -d <db_name> -c \
     "SELECT sale_id, price_contract_id, status FROM sales WHERE sync_status != 'synced' LIMIT 10;"
   ```

2. **Run integrity check:**
   ```bash
   curl -X GET http://api.example.com/api/v1/admin/sync-recovery/check-integrity \
     -H "Authorization: Bearer <token>" | jq .
   ```

3. **Contact support with:**
   - Sale UUIDs that fail to sync
   - Contract UUID in question
   - Error message from browser DevTools or server logs
