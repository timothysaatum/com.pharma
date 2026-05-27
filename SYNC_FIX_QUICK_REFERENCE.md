# Quick Reference: Sync Foreign Key Fix

## What Was Fixed

### 1. Foreign Key Constraint Violations (Error: `sales_price_contract_id_fkey`)
The sync service was only validating `price_contract_id` when creating sales. Now it validates ALL foreign keys:
- ✅ **cashier_id** (required) - Must exist
- ✅ **branch_id** (required) - Must exist
- ✅ **customer_id** (optional) - Cleared if missing
- ✅ **price_contract_id** (optional) - Cleared if missing
- ✅ **pharmacist_id** (optional) - Cleared if missing

### 2. Orphaned/Corrupted Sales Records
Sales that failed to sync or had missing references are now detectable and fixable:
- Detect orphaned records (missing FK references)
- Auto-fix correctable issues
- Flag non-fixable issues for manual intervention
- Generate integrity reports

## Using the New API Endpoints

### Admin Authentication Required
All endpoints require admin role. Include auth token:
```bash
Authorization: Bearer <admin_token>
```

### Check Sync Health (Start Here)
```bash
GET /api/v1/admin/sync-recovery/health
```
Response shows: total sales, sync status distribution, stale pending count

### Scan for Issues
```bash
GET /api/v1/admin/sync-recovery/check-integrity
?max_issues=100
&branch_id=<optional-branch-id>
```
Response: List of detected integrity issues with details

### Get Comprehensive Report
```bash
GET /api/v1/admin/sync-recovery/report
```
Response: Health summary + issues + recommendations

### Auto-Fix Issues (Dry Run First)
```bash
POST /api/v1/admin/sync-recovery/bulk-fix
?dry_run=true
&issue_types=missing_customer
&issue_types=missing_price_contract
&issue_types=missing_pharmacist
&issue_types=stale_pending_sale
```
Dry run shows what would be fixed without making changes

### Apply Fixes
```bash
POST /api/v1/admin/sync-recovery/bulk-fix
?dry_run=false
```
Applies fixes and commits to database

## Understanding Issue Types

### Auto-Fixable ✅
- **missing_customer** - Clears customer_id
- **missing_price_contract** - Clears price_contract_id
- **missing_pharmacist** - Clears pharmacist_id
- **stale_pending_sale** - Marks as synced

### Manual Review Required ⚠️
- **missing_cashier** - Cannot auto-fix (required field)
- **missing_branch** - Cannot auto-fix (required field)
- **invalid_financial_amounts** - Requires reconciliation

## Step-by-Step Recovery

### 1. Check Health
```bash
curl -X GET http://api.example.com/api/v1/admin/sync-recovery/health \
  -H "Authorization: Bearer $TOKEN"
```

### 2. Identify Issues
```bash
curl -X GET http://api.example.com/api/v1/admin/sync-recovery/check-integrity \
  -H "Authorization: Bearer $TOKEN"
```

### 3. Review Report
```bash
curl -X GET http://api.example.com/api/v1/admin/sync-recovery/report \
  -H "Authorization: Bearer $TOKEN"
```

### 4. Dry Run Fix
```bash
curl -X POST http://api.example.com/api/v1/admin/sync-recovery/bulk-fix?dry_run=true \
  -H "Authorization: Bearer $TOKEN"
```

### 5. Apply Fixes
```bash
curl -X POST http://api.example.com/api/v1/admin/sync-recovery/bulk-fix?dry_run=false \
  -H "Authorization: Bearer $TOKEN"
```

### 6. Verify
```bash
curl -X GET http://api.example.com/api/v1/admin/sync-recovery/health \
  -H "Authorization: Bearer $TOKEN"
```

## Code Changes Summary

### File: `sync_service.py`
**New Function:** `_validate_and_fix_sale_fks()`
- Validates all FK fields during sync push
- Clears invalid optional FKs
- Rejects missing required FKs with error

**Enhanced:** `_push_sale()` method
- Calls new FK validation function
- Better error handling and logging
- Prevents constraint violations

### File: `sync_integrity.py` (NEW)
**New Classes:**
- `SyncIntegrityIssue` - Represents detected issue
- `SyncIntegrityService` - Detects and fixes issues

**Key Methods:**
- `check_sale_integrity()` - Scans for issues
- `fix_sale_integrity()` - Auto-fixes correctable issues
- `get_sync_status_summary()` - Health overview

### File: `sync_recovery_endpoints.py` (NEW)
**New Endpoints:**
- Health check
- Integrity scanning
- Issue fixing
- Bulk operations
- Reporting

## Testing the Fix

### Test 1: Validate FK Detection
1. Create a sale with invalid cashier_id
2. Try to sync → Should fail with clear error
3. Check integrity → Should detect missing_cashier

### Test 2: Auto-Fix Optional FK
1. Create a sale with missing price_contract
2. Sync push → Should clear price_contract_id automatically
3. Check integrity → Issue should be fixed

### Test 3: Detect Stale Pending
1. Update a sale to pending state
2. Leave > 24 hours
3. Check integrity → Should detect stale_pending_sale
4. Bulk-fix → Should be marked as synced

## Monitoring

### Key Metrics to Monitor
- `stale_pending_count` - Sales stuck > 24 hours
- `sync_status` distribution - Should be mostly "synced"
- Error-severity issues - Should resolve after fixes

### Recommended Checks
- Daily: Run health check
- Weekly: Full integrity scan
- After fixes: Verify resolved issues

## Common Scenarios

### Scenario 1: Sync Stops with FK Error
**Symptoms:** Sync push fails, sales not synced
**Diagnosis:** Run `check-integrity` endpoint
**Fix:** 
1. Identify missing FK
2. Create missing record on server
3. Retry sync push

### Scenario 2: Sales Visible in Reports but Not on Server
**Symptoms:** Report shows data but can't find in database
**Diagnosis:** Missing FK references
**Fix:**
1. Run `check-integrity` - identify orphaned records
2. Either delete orphaned records or create missing FKs
3. Use `bulk-fix` for auto-fixable issues

### Scenario 3: Sync Seems Stuck
**Symptoms:** Sales in pending state for long time
**Diagnosis:** Run health check → `stale_pending_count > 0`
**Fix:**
1. Investigate why stale pending exists
2. Use `bulk-fix` with `issue_type=stale_pending_sale`
3. Monitor for future stuck sales

## Performance Tips

1. **Run integrity checks during low-traffic hours**
   - Checking scans all sales; can be slow on large datasets

2. **Limit max_issues parameter**
   - Default 100, max 1000
   - Use smaller values for incremental fixes

3. **Batch fixes**
   - Run `bulk-fix` with limited issue types
   - Monitor after each batch

## Additional Resources

- Deployment Guide: `SYNC_FIX_DEPLOYMENT_GUIDE.md`
- Backend Code: `app/services/sync/sync_service.py`
- API Code: `app/api/v1/endpoints/sync_recovery_endpoints.py`
- Data Integrity: `app/services/sync/sync_integrity.py`

## Support

For issues or questions:
1. Check the logs for FK validation messages
2. Run the integrity report
3. Review the deployment guide
4. Contact the engineering team with issue details
