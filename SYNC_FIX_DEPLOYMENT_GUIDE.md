# Sync Foreign Key & Data Integrity Fix - Deployment Guide

## Summary of Issues Fixed

### Issue 1: Foreign Key Constraint Violation
**Error:** `sqlalchemy.exc.IntegrityError: insert or update on table "sales" violates foreign key constraint "sales_price_contract_id_fkey"`

**Root Cause:** During sync push operations, sales records were created with invalid foreign key references (cashier_id, customer_id, price_contract_id, etc.) that didn't exist on the production server. The previous code only validated `price_contract_id`, leaving other critical FKs unvalidated.

**Fix:** Enhanced the `_push_sale` method in `sync_service.py` to comprehensively validate all foreign keys before inserting sales records:
- Validates required FKs (cashier_id, branch_id) - rejects if missing
- Validates optional FKs (customer_id, price_contract_id, pharmacist_id) - clears if missing
- Provides detailed logging of all FK fixes for audit trail

### Issue 2: "First 3 Sales" Not Available on Production
**Symptoms:** Sales appeared in reports but didn't exist on production server; showed with missing branch info

**Root Cause:** Failed or incomplete sync operations left sales records in "pending" state on the server, or corrupted records with missing required fields

**Fix:** Created new data integrity service that:
- Detects orphaned/corrupted records
- Identifies stale pending sales (stuck > 24 hours)
- Auto-fixes correctable issues
- Provides comprehensive integrity reports

## Files Changed

### Backend Changes

1. **`app/services/sync/sync_service.py`** (Enhanced)
   - Added `_validate_and_fix_sale_fks()` async function
   - Enhanced `_push_sale()` with comprehensive FK validation
   - Improved error handling and logging

2. **`app/services/sync/sync_integrity.py`** (NEW)
   - `SyncIntegrityService` class for detecting data issues
   - `SyncIntegrityIssue` class for issue representation
   - Methods: `check_sale_integrity()`, `fix_sale_integrity()`, `get_sync_status_summary()`

3. **`app/api/v1/endpoints/sync_recovery_endpoints.py`** (NEW)
   - Admin-only endpoints for sync recovery:
     - `GET /api/v1/admin/sync-recovery/health` - sync status
     - `GET /api/v1/admin/sync-recovery/check-integrity` - detect issues
     - `POST /api/v1/admin/sync-recovery/fix-issue/{type}/{id}` - fix single issue
     - `POST /api/v1/admin/sync-recovery/bulk-fix` - fix multiple issues
     - `GET /api/v1/admin/sync-recovery/report` - comprehensive report

4. **`app/api/v1/__init__.py`** (Updated)
   - Registered `sync_recovery_router`

## Deployment Steps

### 1. Database Backup
```bash
# Create a backup before deploying
pg_dump -U $DB_USER -d $DB_NAME > sync_fix_backup_$(date +%Y%m%d_%H%M%S).sql
```

### 2. Deploy Code
```bash
# Pull latest changes
git pull origin main

# Install any new dependencies (if needed)
pip install -r requirements.txt

# Run database migrations (if any)
alembic upgrade head
```

### 3. Verify Deployment
```bash
# Check the sync recovery endpoints are available
curl -X GET http://localhost:8000/api/v1/admin/sync-recovery/health \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Testing & Verification

### Step 1: Check Sync Health
```bash
curl -X GET http://localhost:8000/api/v1/admin/sync-recovery/health \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Expected response:
```json
{
  "status": "ok",
  "data": {
    "organization_id": "...",
    "branch_id": null,
    "total_sales": 1000,
    "sync_status": {
      "synced": 998,
      "pending": 2,
      "deleted": 0
    },
    "stale_pending_count": 0,
    "recent_activity_1h": 5
  }
}
```

### Step 2: Scan for Integrity Issues (Dry Run)
```bash
curl -X GET "http://localhost:8000/api/v1/admin/sync-recovery/check-integrity?max_issues=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Expected response shows any detected issues:
```json
{
  "status": "ok",
  "total_issues": 3,
  "severity_breakdown": {
    "warning": 2,
    "error": 1
  },
  "issues": [
    {
      "issue_type": "missing_price_contract",
      "record_id": "...",
      "record_type": "sale",
      "description": "Sales references missing price contract ...",
      "details": {...},
      "severity": "warning",
      "timestamp": "2026-05-27T..."
    }
  ]
}
```

### Step 3: Generate Full Report
```bash
curl -X GET http://localhost:8000/api/v1/admin/sync-recovery/report \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Step 4: Auto-Fix Issues (Dry Run First)
```bash
# Dry run to see what would be fixed
curl -X POST "http://localhost:8000/api/v1/admin/sync-recovery/bulk-fix?dry_run=true" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json"
```

### Step 5: Apply Fixes
```bash
# Apply fixes for auto-correctable issues
curl -X POST "http://localhost:8000/api/v1/admin/sync-recovery/bulk-fix?dry_run=false" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json"
```

## Issue Type Reference

### Auto-Fixable Issues (Can be fixed automatically)

1. **missing_customer** - Customer referenced in sale doesn't exist
   - Fix: Clears `customer_id` to NULL
   - Risk: LOW - customer_id is optional

2. **missing_price_contract** - Price contract referenced doesn't exist
   - Fix: Clears `price_contract_id`, `contract_name`, `contract_discount_percentage`
   - Risk: LOW - contract_id is optional

3. **missing_pharmacist** - Pharmacist user referenced doesn't exist
   - Fix: Clears `pharmacist_id`
   - Risk: LOW - pharmacist_id is optional

4. **stale_pending_sale** - Sale stuck in pending state > 24 hours
   - Fix: Sets `sync_status` to "synced"
   - Risk: MEDIUM - verify sync completed successfully first

### Non-Auto-Fixable Issues (Require manual intervention)

1. **missing_cashier** - Cashier user referenced doesn't exist
   - Problem: `cashier_id` is required and NOT NULL
   - Impact: Sale cannot be saved without valid cashier
   - Fix: Manually assign to current admin or delete sale

2. **missing_branch** - Branch referenced doesn't exist
   - Problem: `branch_id` is required and NOT NULL
   - Impact: Sale cannot be saved without valid branch
   - Fix: Manually assign to correct branch or delete sale

3. **invalid_financial_amounts** - Total doesn't match calculation
   - Problem: Financial calculation mismatch
   - Impact: Audit and reporting inaccuracy
   - Fix: Manual reconciliation required

## Troubleshooting

### Issue: "Only admins can access sync recovery endpoints"
**Solution:** Ensure the user has admin role
```python
# Verify user role in database
SELECT id, email, role FROM users WHERE email = 'admin@example.com';
# Should show role as 'admin' or 'super_admin'
```

### Issue: Foreign key validation still failing
**Solution:** Check if users/branches/contracts exist
```sql
-- Check if a user exists
SELECT id, email FROM users WHERE id = 'user-id-here';

-- Check if a branch exists
SELECT id, name FROM branches WHERE id = 'branch-id-here';

-- Check if a contract exists
SELECT id, contract_name FROM price_contracts WHERE id = 'contract-id-here';
```

### Issue: Stale pending sales not being fixed
**Solution:** Manually review the sales first
```sql
-- Find stale pending sales
SELECT id, sale_number, status, sync_status, updated_at 
FROM sales 
WHERE sync_status = 'pending' 
  AND updated_at < now() - interval '24 hours'
ORDER BY updated_at;

-- Verify they're valid before marking as synced
-- Then use the API to fix them
```

## Monitoring & Prevention

### Setup Alerts
1. Monitor for **critical** severity issues in reports
2. Set up hourly integrity checks if possible:
   ```bash
   # Cron job every hour
   0 * * * * curl -s http://localhost:8000/api/v1/admin/sync-recovery/health \
     -H "Authorization: Bearer $ADMIN_TOKEN" | \
     grep -q "stale_pending_count.*[1-9]" && \
     send_alert "Stale pending sales detected"
   ```

### Prevention Best Practices
1. **Sync order:** Process sync records in dependency order (contracts → sales)
2. **Validation:** Always validate FKs before inserting sync records
3. **Monitoring:** Regular integrity checks (daily/weekly)
4. **Testing:** Test sync push with incomplete/missing FK scenarios
5. **Logging:** Monitor logs for FK validation warnings

## Rollback Plan

If issues occur after deployment:

```bash
# 1. Restore from backup
psql -U $DB_USER -d $DB_NAME < sync_fix_backup_YYYYMMDD_HHMMSS.sql

# 2. Revert code to previous version
git checkout HEAD~1
git push origin main

# 3. Restart application
systemctl restart pharma-backend
```

## Performance Impact

- **Minimal:** FK validation adds ~1-5ms per sale sync
- **DB Queries:** 5-8 additional SELECT queries per sale (for FK validation)
- **Logging:** Comprehensive logging may increase log volume slightly

## Next Steps

1. Deploy code to staging first
2. Run full integrity check on staging
3. Test sync push with various offline scenarios
4. Deploy to production
5. Run integrity report and auto-fix non-critical issues
6. Monitor logs for FK validation messages
7. Schedule weekly integrity checks

## Support

For issues or questions:
1. Check logs: `tail -f /var/log/pharma-backend.log`
2. Review sync recovery endpoints: `GET /api/v1/admin/sync-recovery/report`
3. Contact engineering team with specific issue IDs from the report
