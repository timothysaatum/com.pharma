

from datetime import datetime
from typing import List, Optional
import uuid
from fastapi import Depends, HTTPException, Query, status, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user, get_organization_id
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.schemas.branch_schemas import BranchResponse, BranchWithStats
from app.services.branch.branch_service import BranchService


router = APIRouter(prefix="/stats")

@router.get("/{branch_id}", response_model=BranchWithStats)
async def get_branch_with_stats(
    branch_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get branch with comprehensive statistics
    
    **Includes**:
    - Total inventory items
    - Total inventory value
    - Low stock count
    - Today's sales
    - Month's sales
    - Active users count
    
    **Returns**: Branch with statistics
    
    **Use Case**: Branch dashboard, performance monitoring
    """
    # Check access
    if not current_user.is_super_admin:
        assigned_branch_ids = {str(b) for b in (current_user.assigned_branches or [])}
        if str(branch_id) not in assigned_branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this branch"
            )
    
    stats_data = await BranchService.get_branch_with_stats(
        db=db,
        branch_id=branch_id,
        organization_id=current_user.organization_id
    )
    
    return {
        **BranchResponse.model_validate(stats_data['branch']).model_dump(),
        "total_inventory_items": stats_data['total_inventory_items'],
        "total_inventory_value": stats_data['total_inventory_value'],
        "low_stock_count": stats_data['low_stock_count'],
        "total_sales_today": stats_data['total_sales_today'],
        "total_sales_month": stats_data['total_sales_month'],
        "active_users_count": stats_data['active_users_count']
    }



# ============================================
# Sales Reporting
# ============================================

@router.get(
    "/reports/summary",
    response_model=dict
)
async def get_sales_summary(
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    branch_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id)
):
    """
    Get sales summary report for a date range
    
    **Query Parameters:**
    - start_date: Start of reporting period (required)
    - end_date: End of reporting period (required)
    - branch_id: Filter by branch (optional)
    
    **Returns:**
    - Total sales count
    - Total revenue
    - Average sale amount
    - Total discount given
    - Total tax collected
    - Sales by payment method
    - Top selling items
    """
    from sqlalchemy import select, func, and_
    from app.models.sales.sales_model import Sale
    
    # Validate date range
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date"
        )
    
    # Build base filter
    filters = [
        Sale.organization_id == organization_id,
        Sale.status == 'completed',
        Sale.created_at >= start_date,
        Sale.created_at <= end_date
    ]
    
    if branch_id:
        filters.append(Sale.branch_id == branch_id)
    
    # Get aggregated data
    result = await db.execute(
        select(
            func.count(Sale.id).label('total_sales'),
            func.sum(Sale.total_amount).label('total_revenue'),
            func.sum(Sale.discount_amount).label('total_discount'),
            func.sum(Sale.tax_amount).label('total_tax'),
            func.avg(Sale.total_amount).label('average_sale')
        )
        .where(and_(*filters))
    )
    
    summary = result.one()
    
    # Get sales by payment method
    result = await db.execute(
        select(
            Sale.payment_method,
            func.count(Sale.id).label('count'),
            func.sum(Sale.total_amount).label('amount')
        )
        .where(and_(*filters))
        .group_by(Sale.payment_method)
    )
    
    payment_methods = {
        row.payment_method: {
            "count": row.count,
            "amount": float(row.amount or 0)
        }
        for row in result.all()
    }
    
    return {
        "period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "summary": {
            "total_sales": summary.total_sales or 0,
            "total_revenue": float(summary.total_revenue or 0),
            "average_sale": float(summary.average_sale or 0),
            "total_discount": float(summary.total_discount or 0),
            "total_tax": float(summary.total_tax or 0)
        },
        "payment_methods": payment_methods
    }


@router.get(
    "/reports/top-selling",
    response_model=List[dict]
)
async def get_top_selling_drugs(
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    branch_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id)
):
    """
    Get top selling drugs for a date range
    
    **Query Parameters:**
    - start_date: Start of reporting period
    - end_date: End of reporting period
    - branch_id: Filter by branch (optional)
    - limit: Number of results (default: 10, max: 100)
    """
    from sqlalchemy import select, func, and_
    from app.models.sales.sales_model import Sale, SaleItem
    from app.models.inventory.inventory_model import Drug
    
    filters = [
        Sale.organization_id == organization_id,
        Sale.status == 'completed',
        Sale.created_at >= start_date,
        Sale.created_at <= end_date
    ]
    
    if branch_id:
        filters.append(Sale.branch_id == branch_id)
    
    result = await db.execute(
        select(
            SaleItem.drug_id,
            SaleItem.drug_name,
            func.sum(SaleItem.quantity).label('total_quantity'),
            func.sum(SaleItem.total_price).label('total_revenue'),
            func.count(SaleItem.id).label('sale_count'),
            func.avg(SaleItem.unit_price).label('average_price')
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .where(and_(*filters))
        .group_by(SaleItem.drug_id, SaleItem.drug_name)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(limit)
    )
    
    return [
        {
            "drug_id": str(row.drug_id),
            "drug_name": row.drug_name,
            "total_quantity": row.total_quantity,
            "total_revenue": float(row.total_revenue),
            "sale_count": row.sale_count,
            "average_price": float(row.average_price)
        }
        for row in result.all()
    ]
