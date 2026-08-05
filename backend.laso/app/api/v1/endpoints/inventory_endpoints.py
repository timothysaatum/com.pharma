"""
Inventory Routes
API endpoints for inventory management, stock adjustments, and transfers
"""
from fastapi import APIRouter, Depends, HTTPException, Path, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import uuid

from app.core.deps import (
    get_db, get_current_active_user,
    require_permission
)
from app.models.user.user_model import User
from app.models.inventory.branch_inventory import DrugBatch
from app.services.inventory.inventory_service import InventoryService
from app.schemas.inventory_schemas import (
    BranchInventoryCreate, BranchInventoryResponse, BranchInventoryUpdate,
    BranchInventoryWithDetails, DrugBatchCreate, DrugBatchResponse, DrugBatchUpdate, StockAdjustmentCreate,
    StockAdjustmentResponse, StockTransferCreate, StockTransferResponse,
    LowStockReport, ExpiringBatchReport, InventoryValuationResponse
)
from app.utils.pagination import PaginationParams, PaginatedResponse


router = APIRouter(prefix="/inventory", tags=["Inventory Management"])


def _ensure_branch_access(current_user: User, branch_id: uuid.UUID) -> None:
    """
    Raise 403 unless the user may access `branch_id`.

    Super admins always pass, matching every sibling router (drug, sales,
    prescription, purchase-order endpoints) — inventory was previously the
    one router that locked super admins out of branches they weren't
    explicitly assigned to.
    """
    if current_user.is_super_admin:
        return
    if str(branch_id) not in [str(b) for b in (current_user.assigned_branches or [])]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this branch"
        )


# Branch Inventory Endpoints

@router.get("/branch/{branch_id}", response_model=PaginatedResponse[BranchInventoryWithDetails])
async def get_branch_inventory(
    branch_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    drug_id: Optional[uuid.UUID] = Query(None, description="Filter by specific drug"),
    include_zero_stock: bool = Query(False, description="Include items with zero quantity"),
    search: Optional[str] = Query(None, description="Search drug name or SKU"),
    low_stock_only: bool = Query(False, description="Show only low stock items"),
    drug_type: Optional[str] = Query(None, description="Filter by drug type"),
    current_user: User = Depends(require_permission("view_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get paginated inventory for a branch
    
    **Query Parameters**:
    - page: Page number (default: 1)
    - page_size: Items per page (default: 50, max: 500)
    - drug_id: Optional - Filter for specific drug
    - include_zero_stock: Include items with zero quantity (default: false)
    - search: Search drug name or SKU
    - low_stock_only: Show only items at or below reorder level
    
    **Returns**: Paginated list of inventory items for the branch
    
    **Note**: Only returns inventory for user's assigned branches
    
    **Performance**: Uses pagination to handle large inventories efficiently
    """
    _ensure_branch_access(current_user, branch_id)
    
    result = await InventoryService.get_branch_inventory_paginated(
        db=db,
        branch_id=branch_id,
        pagination=pagination,
        drug_id=drug_id,
        include_zero_stock=include_zero_stock,
        search=search,
        low_stock_only=low_stock_only,
        drug_type=drug_type,
    )
    
    return result


@router.post("/branch/{branch_id}/drugs", response_model=BranchInventoryResponse, status_code=status.HTTP_201_CREATED)
async def add_drug_to_branch(
    branch_id: uuid.UUID,
    inventory_data: BranchInventoryCreate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """Add an organization catalogue drug to a branch's sellable inventory."""
    _ensure_branch_access(current_user, branch_id)
    if inventory_data.branch_id != branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload branch_id must match the URL branch_id"
        )

    inventory = await InventoryService.add_drug_to_branch(
        db=db,
        branch_id=branch_id,
        drug_id=inventory_data.drug_id,
        organization_id=current_user.organization_id,
        location=inventory_data.location,
        selling_price=inventory_data.selling_price,
    )
    return inventory


@router.patch("/branch/{branch_id}/drugs/{drug_id}", response_model=BranchInventoryResponse)
async def update_branch_drug_settings(
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    update_data: BranchInventoryUpdate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """Update branch-specific drug settings such as shelf location and price."""
    _ensure_branch_access(current_user, branch_id)
    inventory = await InventoryService.update_branch_drug_settings(
        db=db,
        branch_id=branch_id,
        drug_id=drug_id,
        location=update_data.location,
        selling_price=update_data.selling_price,
    )
    return inventory


@router.delete("/branch/{branch_id}/drugs/{drug_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_drug_from_branch(
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """Remove a zero-stock drug from this branch's sellable inventory."""
    _ensure_branch_access(current_user, branch_id)
    await InventoryService.remove_drug_from_branch(
        db=db,
        branch_id=branch_id,
        drug_id=drug_id,
    )
    return None


@router.post("/reserve", status_code=status.HTTP_200_OK)
async def reserve_inventory(
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    quantity: int = Query(..., gt=0),
    current_user: User = Depends(require_permission("process_sales")),
    db: AsyncSession = Depends(get_db)
):
    """
    Reserve inventory for pending orders/prescriptions
    
    **Required Permission**: process_sales
    
    **Use Case**: Reserve stock when order is placed but not yet paid
    
    **Returns**: Updated inventory with increased reserved_quantity
    
    **Errors**:
    - 400: Insufficient available stock
    """
    _ensure_branch_access(current_user, branch_id)
    
    inventory = await InventoryService.reserve_inventory(
        db=db,
        branch_id=branch_id,
        drug_id=drug_id,
        quantity=quantity
    )
    
    return {
        "success": True,
        "message": f"Reserved {quantity} units",
        "inventory": BranchInventoryResponse.model_validate(inventory)
    }


@router.post("/release-reserved", status_code=status.HTTP_200_OK)
async def release_reserved_inventory(
    branch_id: uuid.UUID,
    drug_id: uuid.UUID,
    quantity: int = Query(..., gt=0),
    current_user: User = Depends(require_permission("process_sales")),
    db: AsyncSession = Depends(get_db)
):
    """
    Release previously reserved inventory
    
    **Required Permission**: process_sales
    
    **Use Case**: Cancel order or prescription, release reserved stock
    
    **Returns**: Updated inventory with decreased reserved_quantity
    
    **Errors**:
    - 400: Cannot release more than reserved
    """
    _ensure_branch_access(current_user, branch_id)
    
    inventory = await InventoryService.release_reserved_inventory(
        db=db,
        branch_id=branch_id,
        drug_id=drug_id,
        quantity=quantity
    )
    
    return {
        "success": True,
        "message": f"Released {quantity} units from reservation",
        "inventory": BranchInventoryResponse.model_validate(inventory)
    }


# Stock Adjustment Endpoints

@router.post("/adjust", response_model=StockAdjustmentResponse, status_code=status.HTTP_201_CREATED)
async def adjust_inventory(
    adjustment_data: StockAdjustmentCreate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Adjust inventory (damage, expiry, theft, return, correction)
    
    **Required Permission**: manage_inventory
    
    **Adjustment Types**:
    - damage: Damaged goods
    - expired: Expired stock
    - theft: Stolen items
    - return: Customer returns
    - correction: Inventory count correction
    - transfer: Inter-branch transfer (use /transfer endpoint instead)
    
    **Note**: Creates audit trail via StockAdjustment record
    
    **Returns**: Stock adjustment record
    
    **Errors**:
    - 400: Would result in negative stock
    - 404: Inventory not found
    """
    _ensure_branch_access(current_user, adjustment_data.branch_id)

    adjustment, inventory = await InventoryService.adjust_inventory(
        db=db,
        branch_id=adjustment_data.branch_id,
        drug_id=adjustment_data.drug_id,
        quantity_change=adjustment_data.quantity_change,
        adjustment_type=adjustment_data.adjustment_type,
        reason=adjustment_data.reason or "",
        adjusted_by=current_user.id,
        transfer_to_branch_id=adjustment_data.transfer_to_branch_id
    )
    
    return adjustment


@router.post("/transfer", response_model=StockTransferResponse, status_code=status.HTTP_201_CREATED)
async def transfer_stock(
    transfer_data: StockTransferCreate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Transfer stock between branches
    
    **Required Permission**: manage_inventory
    
    **Validations**:
    - User has access to source branch
    - Sufficient available stock at source
    - Source and destination are different
    
    **Process**:
    1. Creates negative adjustment at source branch
    2. Creates positive adjustment at destination branch
    3. Links both adjustments
    
    **Returns**: Both adjustment records (source and destination)
    
    **Errors**:
    - 400: Insufficient stock, same branch, etc.
    - 403: No access to source branch
    - 404: Drug not found at source
    """
    # Check access to source branch
    _ensure_branch_access(current_user, transfer_data.from_branch_id)
    # Check access to destination branch
    _ensure_branch_access(current_user, transfer_data.to_branch_id)

    source_adj, dest_adj = await InventoryService.transfer_stock(
        db=db,
        from_branch_id=transfer_data.from_branch_id,
        to_branch_id=transfer_data.to_branch_id,
        drug_id=transfer_data.drug_id,
        quantity=transfer_data.quantity,
        reason=transfer_data.reason,
        transferred_by=current_user.id
    )
    
    return StockTransferResponse(
        source_adjustment=StockAdjustmentResponse.model_validate(source_adj),
        destination_adjustment=StockAdjustmentResponse.model_validate(dest_adj),
        success=True,
        message=f"Successfully transferred {transfer_data.quantity} units"
    )


# Drug Batch Endpoints

@router.post("/batches", response_model=DrugBatchResponse, status_code=status.HTTP_201_CREATED)
async def create_drug_batch(
    batch_data: DrugBatchCreate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new drug batch
    
    **Required Permission**: manage_inventory
    
    **Use Case**: Receiving new stock from supplier
    
    **Validations**:
    - Batch number unique per drug per branch
    - Expiry date in future
    - Remaining quantity ≤ initial quantity
    
    **Side Effect**: Updates branch inventory quantity
    
    **Returns**: Created batch record
    
    **Errors**:
    - 400: Batch already exists, validation error
    """
    _ensure_branch_access(current_user, batch_data.branch_id)

    batch = await InventoryService.create_batch(
        db=db,
        batch_data=batch_data,
        created_by=current_user.id,
    )
    return batch


@router.get("/batches/drug/{drug_id}", response_model=PaginatedResponse[DrugBatchResponse])
async def get_drug_batches(
    drug_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    branch_id: Optional[uuid.UUID] = Query(None, description="Filter by branch"),
    include_expired: bool = Query(False, description="Include expired batches"),
    include_empty: bool = Query(False, description="Include empty batches"),
    expiring_within_days: Optional[int] = Query(None, ge=1, le=365, description="Show batches expiring within N days"),
    current_user: User = Depends(require_permission("view_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get paginated batches for a drug
    
    **Query Parameters**:
    - page: Page number (default: 1)
    - page_size: Items per page (default: 50, max: 500)
    - branch_id: Optional - Filter by specific branch
    - include_expired: Include expired batches (default: false)
    - include_empty: Include batches with zero quantity (default: false)
    - expiring_within_days: Show only batches expiring within N days
    
    **Ordering**: FEFO (First Expired First Out) - sorted by expiry date
    
    **Returns**: Paginated list of drug batches
    """
    # If branch_id specified, check access; otherwise restrict to assigned branches
    if branch_id:
        _ensure_branch_access(current_user, branch_id)
        branch_ids = None
    elif current_user.is_super_admin:
        # No branch filter — super admins see every branch, same as every
        # other report/list endpoint in this codebase.
        branch_ids = None
    else:
        # Preserve an explicit empty scope.  ``None`` means "do not apply a
        # branch filter" to the service, whereas [] must mean "return no
        # rows" for a user who has no assigned branches.
        assigned = [str(b) for b in (current_user.assigned_branches or [])]
        branch_ids = [uuid.UUID(b) for b in assigned]

    result = await InventoryService.get_batches_paginated(
        db=db,
        drug_id=drug_id,
        pagination=pagination,
        branch_id=branch_id,
        branch_ids=branch_ids,
        include_expired=include_expired,
        include_empty=include_empty,
        expiring_within_days=expiring_within_days
    )
    
    return result


@router.patch("/batches/{batch_id}", response_model=DrugBatchResponse)
async def update_drug_batch(
    batch_id: uuid.UUID,
    batch_data: DrugBatchUpdate,
    current_user: User = Depends(require_permission("manage_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Update an existing drug batch
    
    **Required Permission**: manage_inventory
    
    **Use Case**: Correcting batch information after entry
    
    **Updatable Fields**:
    - batch_number: Unique identifier for batch
    - remaining_quantity: Correcting stock count
    - cost_price: Update cost basis
    - selling_price: Adjust sale price
    - supplier: Correct supplier info
    
    **Returns**: Updated batch record
    
    **Errors**:
    - 404: Batch not found
    """
    batch = await db.get(DrugBatch, batch_id)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found"
        )
    
    _ensure_branch_access(current_user, batch.branch_id)

    batch = await InventoryService.update_batch(db=db, batch_id=batch_id, batch_data=batch_data)
    return batch


@router.post("/batches/{batch_id}/consume", response_model=DrugBatchResponse)
async def consume_from_batch(
    batch_id: uuid.UUID,
    quantity: int = Query(..., gt=0, description="Quantity to consume"),
    current_user: User = Depends(require_permission("process_sales")),
    db: AsyncSession = Depends(get_db)
):
    """
    Consume quantity from a batch (for sales)
    
    **Required Permission**: process_sales
    
    **Security**: Verifies the user has access to the batch's branch
    **Safety**: Rejects consumption from expired batches
    
    **Returns**: Updated batch with reduced remaining_quantity
    
    **Errors**:
    - 400: Insufficient quantity in batch, or batch is expired
    - 403: No access to this batch's branch
    - 404: Batch not found
    """
    # Load batch and verify access
    batch = await db.get(DrugBatch, batch_id)
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found"
        )
    _ensure_branch_access(current_user, batch.branch_id)

    batch = await InventoryService.consume_from_batch(
        db=db,
        batch_id=batch_id,
        quantity=quantity
    )
    
    return batch


# Reporting Endpoints

@router.get("/low-stock", response_model=LowStockReport)
async def get_low_stock_report(
    branch_id: Optional[uuid.UUID] = Query(None, description="Filter by branch"),
    current_user: User = Depends(require_permission("view_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate low stock report

    **Query Parameters**:
    - branch_id: Optional - Filter by specific branch

    **Includes**:
    - Drugs at or below reorder level
    - Out of stock items
    - Recommended reorder quantities

    **Returns**: Comprehensive low stock report

    **Use Case**: 
    - Daily stock monitoring
    - Purchase order planning
    - Alert generation
    """
    # If branch_id specified, check access; otherwise restrict to assigned branches
    if branch_id:
        _ensure_branch_access(current_user, branch_id)
        branch_ids = None
    elif current_user.is_super_admin:
        branch_ids = None
    else:
        # A non-elevated user with no assigned branches must get an empty
        # report, not an unfiltered org-wide one — ``branch_ids=None`` below
        # tells the service to skip the branch filter entirely, so an empty
        # *list* (not None) is required to correctly return zero rows.
        assigned = [str(b) for b in (current_user.assigned_branches or [])]
        branch_ids = [uuid.UUID(b) for b in assigned]

    report = await InventoryService.get_low_stock_report(
        db=db,
        organization_id=current_user.organization_id,
        branch_id=branch_id,
        branch_ids=branch_ids,
    )
    
    return report


@router.get("/expiring/{branch_id}", response_model=ExpiringBatchReport)
async def get_expiring_batches_report(
    branch_id: uuid.UUID = Path(..., description="Filter by branch"),
    days_threshold: int = Query(90, ge=1, le=365, description="Days until expiry threshold"),
    current_user: User = Depends(require_permission("view_inventory")),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate expiring batches report

    **Query Parameters**:
    - branch_id: Optional - Filter by specific branch
    - days_threshold: Days until expiry (default: 90, max: 365)

    **Includes**:
    - Batches expiring within threshold
    - Days until expiry
    - Cost and selling value at risk

    **Returns**: Comprehensive expiring batches report

    **Use Case**:
    - Prevent expiry losses
    - Plan promotions for near-expiry items
    - Regulatory compliance
    """
    # If branch_id specified, check access
    if branch_id:
        _ensure_branch_access(current_user, branch_id)

    report = await InventoryService.get_expiring_batches_report(
        db=db,
        organization_id=current_user.organization_id,
        branch_id=branch_id,
        days_threshold=days_threshold
    )
    
    return report


@router.get("/reports/valuation/{branch_id}", response_model=InventoryValuationResponse)
async def get_inventory_valuation(
    branch_id: uuid.UUID,
    current_user: User = Depends(require_permission("view_reports")),
    db: AsyncSession = Depends(get_db)
):
    """
    Calculate inventory valuation for a branch
    
    **Required Permission**: view_reports
    
    **Calculates**:
    - Total cost value (acquisition cost)
    - Total selling value (potential revenue)
    - Potential profit
    - Profit margin percentage
    
    **Returns**: Detailed valuation report with per-item breakdown
    
    **Use Case**:
    - Financial reporting
    - Balance sheet preparation
    - Business valuation
    - Performance analysis
    """
    # Check access
    _ensure_branch_access(current_user, branch_id)

    valuation = await InventoryService.get_inventory_valuation(
        db=db,
        branch_id=branch_id
    )
    
    return valuation
