"""
Reports API Endpoints
Provides access to analytics and reporting data
"""

from datetime import date
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db
from app.models.user.user_model import User
from app.services.reports.reports_service import ReportsService

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("/daily-sales-summary")
async def get_daily_sales_summary(
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
    
    Query Parameters:
    - start_date: Start date for report (YYYY-MM-DD)
    - end_date: End date for report (YYYY-MM-DD)
    - branch_id: Optional branch filter
    - contract_id: Optional price contract filter
    - cashier_id: Optional cashier/user filter
    
    Returns: List of daily sales records with aggregated amounts and quantities
    """
    return await ReportsService.get_daily_sales_summary(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_id,
        contract_id=contract_id,
        cashier_id=cashier_id,
    )


@router.get("/contract-performance")
async def get_contract_performance(
    start_date: date = Query(...),
    end_date: date = Query(...),
    contract_id: Optional[uuid.UUID] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get contract performance metrics.
    
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


@router.get("/inventory-alerts")
async def get_inventory_alerts(
    branch_id: Optional[uuid.UUID] = Query(None),
    alert_types: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get inventory alerts (low stock, expiring, expired).
    
    Query Parameters:
    - branch_id: Optional branch filter
    - alert_types: Comma-separated types: low_stock,expiring,expired
    
    Returns: List of inventory alerts
    """
    return await ReportsService.get_inventory_alerts(
        db=db,
        organization_id=current_user.organization_id,
        branch_id=branch_id,
        alert_types=alert_types.split(",") if alert_types else None,
    )


@router.get("/top-customers")
async def get_top_customers(
    start_date: date = Query(...),
    end_date: date = Query(...),
    limit: int = Query(10),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get top customers by revenue.
    
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


@router.get("/drug-turnover")
async def get_drug_turnover(
    start_date: date = Query(...),
    end_date: date = Query(...),
    branch_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(20),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get drug turnover metrics (units sold, revenue).
    
    Query Parameters:
    - start_date: Start date for report
    - end_date: End date for report
    - branch_id: Optional branch filter
    - limit: Number of drugs to return (default: 20)
    
    Returns: Drug turnover metrics
    """
    return await ReportsService.get_drug_turnover(
        db=db,
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_id,
        limit=limit,
    )
