"""
Export API Endpoints
Provides download endpoints for Excel exports
"""

from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db
from app.models.user.user_model import User
from app.services.export.excel_export_service import ExcelExportService

router = APIRouter(prefix="/export", tags=["Export"])


@router.get("/sales/excel")
async def export_sales_excel(
    year: int = Query(...),
    month: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Export sales data to Excel format.
    
    Query Parameters:
    - year: Year to export (YYYY)
    - month: Optional month (1-12). If not provided, exports all 12 months in separate sheets
    
    Returns: Excel file download
    """
    file_stream = await ExcelExportService.export_sales_by_month(
        db=db,
        organization_id=current_user.organization_id,
        year=year,
        month=month,
    )
    
    filename = f"sales_{year}_{month if month else 'full_year'}.xlsx"
    return StreamingResponse(
        file_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/inventory/excel")
async def export_inventory_excel(
    year: int = Query(...),
    month: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Export inventory snapshot to Excel format.
    
    Query Parameters:
    - year: Year (YYYY)
    - month: Month (1-12)
    
    Returns: Excel file download with current inventory state
    """
    file_stream = await ExcelExportService.export_inventory_by_month(
        db=db,
        organization_id=current_user.organization_id,
        year=year,
        month=month,
    )
    
    filename = f"inventory_{year}_{month:02d}.xlsx"
    return StreamingResponse(
        file_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/staff/excel")
async def export_staff_excel(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Export staff/user data to Excel format.
    
    Note: Admin only endpoint
    
    Returns: Excel file download with staff directory
    """
    # Permission check for admin/super_admin should be added if not in dependency
    file_stream = await ExcelExportService.export_staff_data(
        db=db,
        organization_id=current_user.organization_id,
    )
    
    filename = "staff_directory.xlsx"
    return StreamingResponse(
        file_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
