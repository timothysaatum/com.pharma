"""
Reports API Endpoints
Provides access to analytics and reporting data
"""

from datetime import date
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, require_permission
from app.models.user.user_model import User
from app.services.reports.reports_service import ReportsService
from app.utils.pagination import PaginatedResponse, PaginationParams
from app.schemas.reports_schemas import DailySalesSummaryRow, DrugTurnoverRow

router = APIRouter(prefix="/reports", tags=["Reports"])


def _resolve_branch_filter(
    branch_id: Optional[uuid.UUID],
    user: User,
) -> tuple:
    """
    Validate branch access and return (branch_id, branch_ids) for service calls.

    - If branch_id is given and user has access: pass branch_id through.
    - If branch_id is given but user lacks access: 403.
    - If branch_id is not given: restrict to the user's assigned branches
      (pass None for branch_id and the assigned list as branch_ids).
    - If user has no assigned branches (super_admin etc.): pass None for both.
    """
    assigned = [str(b) for b in (user.assigned_branches or [])]
    if branch_id:
        if str(branch_id) not in assigned:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this branch",
            )
        return branch_id, None
    if assigned:
        return None, [uuid.UUID(b) for b in assigned]
    return None, None


@router.get(
    "/daily-sales-summary",
    response_model=PaginatedResponse[DailySalesSummaryRow],
    dependencies=[Depends(require_permission("view_reports"))],
)
async def get_daily_sales_summary(
    pagination: PaginationParams = Depends(),
    start_date: date = Query(...),
    end_date: date = Query(...),
    branch_id: Optional[uuid.UUID] = Query(None),
    contract_id: Optional[uuid.UUID] = Query(None),
    cashier_id: Optional[uuid.UUID] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get daily sales summary with filtering options.

    **Required Permission:** view_reports

    Query Parameters:
    - start_date: Start date for report (YYYY-MM-DD)
    - end_date: End date for report (YYYY-MM-DD)
    - branch_id: Optional branch filter (restricted to assigned branches)
    - contract_id: Optional price contract filter
    - cashier_id: Optional cashier/user filter

    Returns: List of daily sales records with aggregated amounts and quantities
    """
    resolved_branch_id, branch_ids = _resolve_branch_filter(branch_id, current_user)
    return await ReportsService.get_daily_sales_summary(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=resolved_branch_id,
        branch_ids=branch_ids,
        contract_id=contract_id,
        cashier_id=cashier_id,
        pagination=pagination,
    )


@router.get(
    "/contract-performance",
    dependencies=[Depends(require_permission("view_reports"))],
)
async def get_contract_performance(
    start_date: date = Query(...),
    end_date: date = Query(...),
    contract_id: Optional[uuid.UUID] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get contract performance metrics.

    **Required Permission:** view_reports

    Query Parameters:
    - start_date: Start date for report
    - end_date: End date for report
    - contract_id: Optional specific contract filter

    Returns: Performance metrics including revenue, discounts, customer count
    """
    return await ReportsService.get_contract_performance(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        contract_id=contract_id,
    )


@router.get(
    "/inventory-alerts",
    dependencies=[Depends(require_permission("view_reports"))],
)
async def get_inventory_alerts(
    branch_id: Optional[uuid.UUID] = Query(None),
    alert_types: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get inventory alerts (low stock, expiring, expired).

    **Required Permission:** view_reports

    Query Parameters:
    - branch_id: Optional branch filter (restricted to assigned branches)
    - alert_types: Comma-separated types: low_stock,expiring,expired

    Returns: List of inventory alerts
    """
    resolved_branch_id, _ = _resolve_branch_filter(branch_id, current_user)
    return await ReportsService.get_inventory_alerts(
        db=db,
        organization_id=current_user.organization_id,
        branch_id=resolved_branch_id,
        alert_types=alert_types.split(",") if alert_types else None,
    )


@router.get(
    "/top-customers",
    dependencies=[Depends(require_permission("view_reports"))],
)
async def get_top_customers(
    start_date: date = Query(...),
    end_date: date = Query(...),
    limit: int = Query(10),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get top customers by revenue.

    **Required Permission:** view_reports

    Query Parameters:
    - start_date: Start date for report
    - end_date: End date for report
    - limit: Number of top customers to return (default: 10)

    Returns: Top customers with revenue and transaction data
    """
    return await ReportsService.get_top_customers(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.get(
    "/drug-turnover",
    response_model=PaginatedResponse[DrugTurnoverRow],
    dependencies=[Depends(require_permission("view_reports"))],
)
async def get_drug_turnover(
    pagination: PaginationParams = Depends(),
    start_date: date = Query(...),
    end_date: date = Query(...),
    branch_id: Optional[uuid.UUID] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get drug turnover metrics (units sold, revenue).

    **Required Permission:** view_reports

    Query Parameters:
    - start_date: Start date for report
    - end_date: End date for report
    - branch_id: Optional branch filter (restricted to assigned branches)

    Returns: Drug turnover metrics
    """
    resolved_branch_id, branch_ids = _resolve_branch_filter(branch_id, current_user)
    return await ReportsService.get_drug_turnover(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=resolved_branch_id,
        branch_ids=branch_ids,
        pagination=pagination,
    )
