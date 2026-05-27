"""
Sync Data Integrity Service
============================
Utilities for detecting and fixing data integrity issues from failed syncs:
- Orphaned records (missing FK references)
- Corrupted sync states (pending records that should be synced)
- Incomplete sales (missing required fields)
- Sync status validation

Used to detect and repair issues causing "sales not available on production"
and other sync data consistency problems.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import uuid

from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales.sales_model import Sale, SaleItem
from app.models.customer.customer_model import Customer
from app.models.pricing.pricing_model import PriceContract
from app.models.user.user_model import User
from app.models.pharmacy.pharmacy_model import Branch

logger = logging.getLogger(__name__)


class SyncIntegrityIssue:
    """Represents a single data integrity issue."""
    
    def __init__(
        self,
        issue_type: str,
        record_id: uuid.UUID,
        record_type: str,
        description: str,
        details: Dict[str, Any],
        severity: str = "warning",  # critical, error, warning, info
    ):
        self.issue_type = issue_type
        self.record_id = record_id
        self.record_type = record_type
        self.description = description
        self.details = details
        self.severity = severity
        self.timestamp = datetime.now(timezone.utc)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_type": self.issue_type,
            "record_id": str(self.record_id),
            "record_type": self.record_type,
            "description": self.description,
            "details": self.details,
            "severity": self.severity,
            "timestamp": self.timestamp.isoformat(),
        }


class SyncIntegrityService:
    """Service for detecting and fixing sync data integrity issues."""

    @staticmethod
    async def check_sale_integrity(
        db: AsyncSession,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        max_issues: int = 100,
    ) -> List[SyncIntegrityIssue]:
        """
        Check sales records for integrity issues.
        
        Detects:
        1. Missing customer (when customer_id is not null)
        2. Missing price contract (when price_contract_id is not null)
        3. Missing cashier user
        4. Missing pharmacist user (when pharmacist_id is not null)
        5. Missing branch
        6. Sales stuck in "pending" state too long
        7. Sales with invalid financial amounts
        
        Returns:
            List of detected issues up to max_issues
        """
        issues: List[SyncIntegrityIssue] = []
        
        # Query all sales for the organization
        conditions = [Sale.organization_id == organization_id]
        if branch_id:
            conditions.append(Sale.branch_id == branch_id)
        
        stmt = select(Sale).where(and_(*conditions))
        result = await db.execute(stmt)
        sales = list(result.scalars().all())
        
        logger.info(
            "Integrity check: scanning %d sales in org %s branch %s",
            len(sales), organization_id, branch_id,
        )
        
        for sale in sales:
            if len(issues) >= max_issues:
                break
            
            # Check 1: Missing customer
            if sale.customer_id is not None:
                customer_exists = await db.execute(
                    select(Customer.id).where(Customer.id == sale.customer_id)
                )
                if customer_exists.scalar_one_or_none() is None:
                    issues.append(SyncIntegrityIssue(
                        issue_type="missing_customer",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sales references missing customer {sale.customer_id}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "customer_id": str(sale.customer_id),
                            "status": sale.status,
                            "sync_status": sale.sync_status,
                        },
                        severity="warning",
                    ))
            
            # Check 2: Missing price contract
            if sale.price_contract_id is not None:
                contract_exists = await db.execute(
                    select(PriceContract.id).where(
                        PriceContract.id == sale.price_contract_id
                    )
                )
                if contract_exists.scalar_one_or_none() is None:
                    issues.append(SyncIntegrityIssue(
                        issue_type="missing_price_contract",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sales references missing price contract {sale.price_contract_id}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "price_contract_id": str(sale.price_contract_id),
                            "contract_name": sale.contract_name,
                            "sync_status": sale.sync_status,
                        },
                        severity="warning",
                    ))
            
            # Check 3: Missing cashier user
            if sale.cashier_id is not None:
                cashier_exists = await db.execute(
                    select(User.id).where(User.id == sale.cashier_id)
                )
                if cashier_exists.scalar_one_or_none() is None:
                    issues.append(SyncIntegrityIssue(
                        issue_type="missing_cashier",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sale references missing cashier user {sale.cashier_id}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "cashier_id": str(sale.cashier_id),
                            "sync_status": sale.sync_status,
                        },
                        severity="error",
                    ))
            
            # Check 4: Missing pharmacist user
            if sale.pharmacist_id is not None:
                pharmacist_exists = await db.execute(
                    select(User.id).where(User.id == sale.pharmacist_id)
                )
                if pharmacist_exists.scalar_one_or_none() is None:
                    issues.append(SyncIntegrityIssue(
                        issue_type="missing_pharmacist",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sale references missing pharmacist user {sale.pharmacist_id}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "pharmacist_id": str(sale.pharmacist_id),
                            "sync_status": sale.sync_status,
                        },
                        severity="warning",
                    ))
            
            # Check 5: Missing branch
            if sale.branch_id is not None:
                branch_exists = await db.execute(
                    select(Branch.id).where(Branch.id == sale.branch_id)
                )
                if branch_exists.scalar_one_or_none() is None:
                    issues.append(SyncIntegrityIssue(
                        issue_type="missing_branch",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sale references missing branch {sale.branch_id}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "branch_id": str(sale.branch_id),
                            "sync_status": sale.sync_status,
                        },
                        severity="critical",
                    ))
            
            # Check 6: Sales stuck in pending state
            if sale.sync_status == "pending":
                age = datetime.now(timezone.utc) - sale.updated_at
                if age > timedelta(hours=24):
                    issues.append(SyncIntegrityIssue(
                        issue_type="stale_pending_sale",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sale stuck in pending state for {age.total_seconds() / 3600:.1f} hours",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "sync_status": sale.sync_status,
                            "updated_at": sale.updated_at.isoformat(),
                            "age_hours": age.total_seconds() / 3600,
                        },
                        severity="error",
                    ))
            
            # Check 7: Invalid financial amounts
            if sale.total_amount is not None:
                expected_total = (sale.subtotal or 0) - (sale.discount_amount or 0) + (sale.tax_amount or 0)
                # Allow for small floating point discrepancies (< $0.01)
                if abs(float(sale.total_amount) - expected_total) > 0.01:
                    issues.append(SyncIntegrityIssue(
                        issue_type="invalid_financial_amounts",
                        record_id=sale.id,
                        record_type="sale",
                        description=f"Sale total_amount mismatch: {sale.total_amount} != {expected_total}",
                        details={
                            "sale_id": str(sale.id),
                            "sale_number": sale.sale_number,
                            "subtotal": float(sale.subtotal or 0),
                            "discount_amount": float(sale.discount_amount or 0),
                            "tax_amount": float(sale.tax_amount or 0),
                            "total_amount": float(sale.total_amount or 0),
                            "expected_total": expected_total,
                            "sync_status": sale.sync_status,
                        },
                        severity="warning",
                    ))
        
        logger.info(
            "Integrity check completed: found %d issues (severity: %s)",
            len(issues),
            ", ".join(set(i.severity for i in issues)) if issues else "none",
        )
        
        return issues

    @staticmethod
    async def fix_sale_integrity(
        db: AsyncSession,
        organization_id: uuid.UUID,
        issue: SyncIntegrityIssue,
    ) -> bool:
        """
        Attempt to fix a detected integrity issue.
        
        Fixes applied by issue type:
        - missing_customer: Clear customer_id to None
        - missing_price_contract: Clear price_contract_id to None
        - missing_pharmacist: Clear pharmacist_id to None
        - missing_cashier: Cannot fix (data loss); requires manual intervention
        - missing_branch: Cannot fix (data loss); requires manual intervention
        - stale_pending_sale: Mark as synced to unblock
        
        Returns:
            True if fix was applied, False if issue cannot be auto-fixed
        """
        sale = await db.get(Sale, issue.record_id)
        if not sale:
            logger.warning(
                "Cannot fix issue: sale %s not found", issue.record_id,
            )
            return False
        
        try:
            if issue.issue_type == "missing_customer":
                sale.customer_id = None
                sale.updated_at = datetime.now(timezone.utc)
                logger.info(
                    "Fixed: cleared customer_id for sale %s", sale.id,
                )
                return True
            
            elif issue.issue_type == "missing_price_contract":
                sale.price_contract_id = None
                sale.contract_name = None
                sale.contract_discount_percentage = None
                sale.updated_at = datetime.now(timezone.utc)
                logger.info(
                    "Fixed: cleared price_contract for sale %s", sale.id,
                )
                return True
            
            elif issue.issue_type == "missing_pharmacist":
                sale.pharmacist_id = None
                sale.updated_at = datetime.now(timezone.utc)
                logger.info(
                    "Fixed: cleared pharmacist_id for sale %s", sale.id,
                )
                return True
            
            elif issue.issue_type == "stale_pending_sale":
                sale.sync_status = "synced"
                sale.updated_at = datetime.now(timezone.utc)
                logger.info(
                    "Fixed: marked stale pending sale %s as synced", sale.id,
                )
                return True
            
            else:
                # Cannot auto-fix critical issues like missing cashier/branch
                logger.warning(
                    "Cannot auto-fix issue type %s for sale %s: requires manual intervention",
                    issue.issue_type, sale.id,
                )
                return False
        
        except Exception as exc:
            logger.error(
                "Error fixing issue %s for sale %s: %s",
                issue.issue_type, sale.id, exc,
                exc_info=True,
            )
            return False

    @staticmethod
    async def get_sync_status_summary(
        db: AsyncSession,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Get summary of sync status for all sales in the organization/branch.
        
        Returns stats on:
        - Total sales
        - Sales by sync_status
        - Recent sync activity
        - Stale/stuck records
        """
        conditions = [Sale.organization_id == organization_id]
        if branch_id:
            conditions.append(Sale.branch_id == branch_id)
        
        # Get sync status distribution
        stmt = select(
            Sale.sync_status,
            func.count(Sale.id).label("count"),
            func.max(Sale.updated_at).label("last_updated"),
        ).where(and_(*conditions)).group_by(Sale.sync_status)
        
        result = await db.execute(stmt)
        status_rows = result.fetchall()
        
        status_summary = {
            "synced": 0,
            "pending": 0,
            "deleted": 0,
        }
        
        for row in status_rows:
            status_summary[row.sync_status] = row.count
        
        # Check for stale pending records
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        stale_stmt = select(func.count(Sale.id)).where(
            and_(
                *conditions,
                Sale.sync_status == "pending",
                Sale.updated_at < stale_threshold,
            )
        )
        stale_result = await db.execute(stale_stmt)
        stale_count = stale_result.scalar() or 0
        
        # Get total and recent sales
        total_stmt = select(func.count(Sale.id)).where(and_(*conditions))
        total_result = await db.execute(total_stmt)
        total_count = total_result.scalar() or 0
        
        recent_threshold = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_stmt = select(func.count(Sale.id)).where(
            and_(
                *conditions,
                Sale.updated_at >= recent_threshold,
            )
        )
        recent_result = await db.execute(recent_stmt)
        recent_count = recent_result.scalar() or 0
        
        return {
            "organization_id": str(organization_id),
            "branch_id": str(branch_id) if branch_id else None,
            "total_sales": total_count,
            "sync_status": status_summary,
            "stale_pending_count": stale_count,
            "recent_activity_1h": recent_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
