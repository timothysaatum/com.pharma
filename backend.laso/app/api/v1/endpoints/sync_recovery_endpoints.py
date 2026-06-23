"""
Sync Recovery Endpoints
=======================
Admin endpoints for checking and fixing sync data integrity issues.

These endpoints allow administrators to:
1. Scan for data integrity issues
2. Get sync status summary
3. Auto-fix correctable issues
4. Generate integrity reports

Used for troubleshooting "sales not available" and other sync problems.
"""

from typing import List, Optional, Dict, Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, get_current_user, require_permission
from app.models.user.user_model import User
from app.services.sync.sync_integrity import (
    SyncIntegrityService,
    SyncIntegrityIssue,
)

router = APIRouter(
    prefix="/admin/sync-recovery",
    tags=["sync-recovery"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/health")
async def get_sync_health(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("view_reports")),
    branch_id: Optional[uuid.UUID] = Query(None),
):
    """
    Get sync health summary for the organization.
    
    Shows:
    - Total sales count
    - Distribution by sync status
    - Stale pending records
    - Recent sync activity
    """
    
    summary = await SyncIntegrityService.get_sync_status_summary(
        db,
        current_user.organization_id,
        branch_id=branch_id,
    )
    
    return {
        "status": "ok",
        "data": summary,
    }


@router.get("/check-integrity")
async def check_integrity(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("view_reports")),
    branch_id: Optional[uuid.UUID] = Query(None),
    max_issues: int = Query(100, ge=1, le=1000),
):
    """
    Scan for data integrity issues in sales records.
    
    Detects:
    - Missing customer/contract/cashier/pharmacist references
    - Missing branch assignment
    - Sales stuck in pending state
    - Invalid financial amounts
    
    Returns:
    - List of detected issues
    - Issue type, severity, and details
    - Record IDs for further investigation
    """
    
    issues = await SyncIntegrityService.check_sale_integrity(
        db,
        current_user.organization_id,
        branch_id=branch_id,
        max_issues=max_issues,
    )
    
    # Group by severity for summary
    severity_counts = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
    
    return {
        "status": "ok",
        "total_issues": len(issues),
        "severity_breakdown": severity_counts,
        "issues": [issue.to_dict() for issue in issues],
    }


@router.post("/fix-issue/{issue_type}/{record_id}")
async def fix_issue(
    issue_type: str,
    record_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("manage_inventory")),
):
    """
    Attempt to auto-fix a detected integrity issue.
    
    Fixable issue types:
    - missing_customer: Clears customer_id
    - missing_price_contract: Clears price_contract_id
    - missing_pharmacist: Clears pharmacist_id
    - stale_pending_sale: Marks as synced
    
    Non-fixable (manual required):
    - missing_cashier: Requires manual intervention (data loss risk)
    - missing_branch: Requires manual intervention (data loss risk)
    - invalid_financial_amounts: Requires manual reconciliation
    
    Returns:
    - success: True if fixed, False if cannot be auto-fixed
    - reason: Explanation if cannot fix
    """
    
    # Create a synthetic issue object for fixing
    issue = SyncIntegrityIssue(
        issue_type=issue_type,
        record_id=record_id,
        record_type="sale",
        description=f"Auto-fix for {issue_type}",
        details={},
    )
    
    fixed = await SyncIntegrityService.fix_sale_integrity(
        db,
        current_user.organization_id,
        issue,
    )
    
    if fixed:
        await db.commit()
        return {
            "status": "ok",
            "success": True,
            "message": f"Successfully fixed {issue_type} for record {record_id}",
        }
    else:
        return {
            "status": "ok",
            "success": False,
            "message": f"Cannot auto-fix {issue_type}; manual intervention required",
        }


@router.post("/bulk-fix")
async def bulk_fix_issues(
    issue_types: List[str] = Query(
        ["missing_customer", "missing_price_contract", "missing_pharmacist", "stale_pending_sale"],
        description="List of issue types to auto-fix",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("manage_inventory")),
    branch_id: Optional[uuid.UUID] = Query(None),
    dry_run: bool = Query(False, description="If true, report what would be fixed without applying"),
):
    """
    Bulk fix all auto-fixable issues for the organization.
    
    Scans for integrity issues and applies fixes for specified types.
    
    Parameters:
    - issue_types: List of issue types to fix (default: all auto-fixable types)
    - dry_run: If true, reports what would be fixed without applying changes
    
    Returns:
    - total_issues_found: Number of issues detected
    - total_fixed: Number of issues fixed
    - by_type: Breakdown of fixes by issue type
    - details: If dry_run, lists what would be fixed
    """
    
    # Get all issues
    issues = await SyncIntegrityService.check_sale_integrity(
        db,
        current_user.organization_id,
        branch_id=branch_id,
        max_issues=10000,
    )
    
    # Filter to requested types
    fixable_issues = [
        i for i in issues
        if i.issue_type in issue_types and i.issue_type in [
            "missing_customer",
            "missing_price_contract",
            "missing_pharmacist",
            "stale_pending_sale",
        ]
    ]
    
    fixed_count = 0
    fixed_by_type: Dict[str, int] = {}
    fix_details = []
    
    if not dry_run:
        for issue in fixable_issues:
            if await SyncIntegrityService.fix_sale_integrity(db, current_user.organization_id, issue):
                fixed_count += 1
                fixed_by_type[issue.issue_type] = fixed_by_type.get(issue.issue_type, 0) + 1
        
        await db.commit()
    else:
        # Dry run: just count what would be fixed
        for issue in fixable_issues:
            fixed_by_type[issue.issue_type] = fixed_by_type.get(issue.issue_type, 0) + 1
            fix_details.append(issue.to_dict())
        fixed_count = len(fixable_issues)
    
    return {
        "status": "ok",
        "dry_run": dry_run,
        "total_issues_found": len(issues),
        "fixable_issues": len(fixable_issues),
        "total_fixed": fixed_count if not dry_run else None,
        "by_type": fixed_by_type,
        "details": fix_details if dry_run else None,
        "message": (
            f"Dry run: would fix {fixed_count} issues"
            if dry_run
            else f"Fixed {fixed_count} issues"
        ),
    }


@router.get("/report")
async def generate_integrity_report(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("view_reports")),
    branch_id: Optional[uuid.UUID] = Query(None),
):
    """
    Generate a comprehensive sync integrity report.
    
    Combines:
    - Sync health summary
    - Detected integrity issues
    - Recommendations
    
    Returns:
    - health_summary: Sync status overview
    - issues: Detected problems grouped by severity
    - recommendations: Suggested fixes
    - export_url: URL to download full report as CSV
    """
    
    # Get health summary
    summary = await SyncIntegrityService.get_sync_status_summary(
        db,
        current_user.organization_id,
        branch_id=branch_id,
    )
    
    # Get issues
    issues = await SyncIntegrityService.check_sale_integrity(
        db,
        current_user.organization_id,
        branch_id=branch_id,
        max_issues=1000,
    )
    
    # Group issues by severity
    issues_by_severity = {
        "critical": [],
        "error": [],
        "warning": [],
        "info": [],
    }
    
    for issue in issues:
        issues_by_severity[issue.severity].append(issue.to_dict())
    
    # Generate recommendations based on issues found
    recommendations = []
    
    if issues_by_severity["critical"]:
        recommendations.append({
            "priority": "high",
            "issue": "Critical data integrity issues detected",
            "action": "Immediately investigate and fix critical issues using /fix-issue endpoint",
        })
    
    if summary["stale_pending_count"] > 0:
        recommendations.append({
            "priority": "high",
            "issue": f"{summary['stale_pending_count']} sales stuck in pending state > 24h",
            "action": "Run bulk-fix with issue_type=stale_pending_sale to resolve",
        })
    
    if issues_by_severity["error"]:
        recommendations.append({
            "priority": "medium",
            "issue": f"{len(issues_by_severity['error'])} error-severity issues found",
            "action": "Investigate and manually fix non-auto-fixable issues",
        })
    
    if issues_by_severity["warning"]:
        recommendations.append({
            "priority": "low",
            "issue": f"{len(issues_by_severity['warning'])} warning-severity issues found",
            "action": "Review and fix when convenient using bulk-fix endpoint",
        })
    
    return {
        "status": "ok",
        "timestamp": summary["timestamp"],
        "organization_id": str(current_user.organization_id),
        "branch_id": str(branch_id) if branch_id else None,
        "health_summary": summary,
        "issues": {
            "critical": len(issues_by_severity["critical"]),
            "error": len(issues_by_severity["error"]),
            "warning": len(issues_by_severity["warning"]),
            "info": len(issues_by_severity["info"]),
            "total": len(issues),
            "by_severity": issues_by_severity,
        },
        "recommendations": recommendations,
    }
