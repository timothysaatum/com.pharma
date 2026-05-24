"""
Price Contract Routes
API endpoints for managing price contracts
"""
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import uuid

from app.core.deps import get_current_user, require_role
from app.db.dependencies import get_db
from app.models.user.user_model import User

from app.schemas.price_contract_schemas import (
    PriceContractCreate,
    PriceContractUpdate,
    PriceContractResponse,
    PriceContractWithDetails,
    PriceContractListResponse,
    PriceContractFilters,
    ApproveContractRequest,
    SuspendContractRequest,
    ActivateContractRequest
)
from app.services.contracts.price_contract_service import PriceContractService



router = APIRouter(prefix="/contracts", tags=["Price Contracts"])


# ============================================
# CREATE CONTRACT
# ============================================

@router.post(
    "",
    response_model=PriceContractResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create new price contract"
)
async def create_contract(
    contract_data: PriceContractCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Create a new price contract.
    
    **Required Role:** Admin, Manager, or Super Admin
    
    **Validations:**
    - Contract code must be unique within organization
    - Only one default contract allowed per organization
    - Insurance contracts require valid insurance_provider_id
    - Branch IDs must exist and belong to organization
    
    **Example:**
    ```json
    {
        "contract_code": "GLICO-STD",
        "contract_name": "GLICO Insurance Standard Plan",
        "contract_type": "insurance",
        "discount_percentage": 10.00,
        "insurance_provider_id": "uuid-here",
        "copay_percentage": 15.00,
        "effective_from": "2024-01-01",
        "status": "draft"
    }
    ```
    """
    contract = await PriceContractService.create_contract(
        db=db,
        contract_data=contract_data,
        user=current_user
    )
    
    return contract


# ============================================
# READ CONTRACTS (LIST)
# ============================================

@router.get(
    "",
    response_model=PriceContractListResponse,
    summary="List all price contracts"
)
async def list_contracts(
    # Filters
    contract_type: Optional[str] = Query(None, description="Filter by type: insurance, staff, corporate, etc."),
    status: Optional[str] = Query(None, description="Filter by status: draft, active, suspended, etc."),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    is_default: Optional[bool] = Query(None, description="Filter default contracts"),
    insurance_provider_id: Optional[uuid.UUID] = Query(None, description="Filter by insurance provider"),
    branch_id: Optional[uuid.UUID] = Query(None, description="Find contracts applicable to branch"),
    search: Optional[str] = Query(None, min_length=1, max_length=100, description="Search in code or name"),
    valid_on_date: Optional[str] = Query(None, description="Find contracts valid on date (YYYY-MM-DD)"),
    
    # Sorting
    sort_by: Optional[str] = Query('created_at', description="Sort by: created_at, contract_name, discount_percentage"),
    sort_order: Optional[str] = Query('desc', description="Sort order: asc or desc"),
    
    # Pagination
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    
    # Dependencies
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get list of price contracts with filtering and pagination.
    
    **Filters:**
    - `contract_type`: insurance, corporate, staff, senior_citizen, standard, wholesale
    - `status`: draft, active, suspended, expired, cancelled
    - `is_active`: true/false
    - `is_default`: true/false (only one default per organization)
    - `insurance_provider_id`: Filter insurance contracts by provider
    - `branch_id`: Find contracts applicable to specific branch
    - `search`: Search in contract code or name
    - `valid_on_date`: Find contracts valid on specific date
    
    **Sorting:**
    - `sort_by`: created_at, contract_name, discount_percentage, total_transactions, effective_from
    - `sort_order`: asc, desc
    
    **Returns:**
    - List of contracts matching filters
    - Total count
    - Pagination info
    """
    # Build filters
    # Parse date string if provided
    valid_date = None
    if valid_on_date:
        try:
            valid_date = date.fromisoformat(valid_on_date)
        except (ValueError, TypeError):
            valid_date = None
    
    filters = PriceContractFilters(
        contract_type=contract_type,
        status=status,
        is_active=is_active,
        is_default=is_default,
        insurance_provider_id=insurance_provider_id,
        branch_id=branch_id,
        user_role=None,
        search=search,
        valid_on_date=valid_date,
        expiring_within_days=None,
        min_discount=None,
        max_discount=None,
        requires_verification=None,
        requires_approval=None,
        created_by=None,
        approved_by=None,
        min_usage_count=None,
        used_in_last_days=None,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size
    )
    
    contracts, total = await PriceContractService.get_contracts(
        db=db,
        user=current_user,
        filters=filters,
        page=page,
        page_size=page_size
    )
    
    # Calculate summary statistics
    total_pages = (total + page_size - 1) // page_size
    
    # Get contract status counts
    from sqlalchemy import select, func
    from app.models.pricing.pricing_model import PriceContract
    
    result = await db.execute(
        select(func.count())
        .select_from(PriceContract)
        .where(
            PriceContract.organization_id == current_user.organization_id,
            PriceContract.is_deleted == False,
            PriceContract.status == 'active'
        )
    )
    total_active_contracts = result.scalar() or 0
    
    result = await db.execute(
        select(func.count())
        .select_from(PriceContract)
        .where(
            PriceContract.organization_id == current_user.organization_id,
            PriceContract.is_deleted == False,
            PriceContract.status == 'suspended'
        )
    )
    total_suspended_contracts = result.scalar() or 0
    
    result = await db.execute(
        select(func.count())
        .select_from(PriceContract)
        .where(
            PriceContract.organization_id == current_user.organization_id,
            PriceContract.is_deleted == False,
            PriceContract.status == 'expired'
        )
    )
    total_expired_contracts = result.scalar() or 0
    
    return PriceContractListResponse(
        contracts=[PriceContractResponse.model_validate(c) for c in contracts],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        total_active_contracts=total_active_contracts,
        total_suspended_contracts=total_suspended_contracts,
        total_expired_contracts=total_expired_contracts
    )


# ============================================
# READ CONTRACT (SINGLE)
# ============================================

@router.get(
    "/{contract_id}",
    response_model=PriceContractResponse,
    summary="Get single contract"
)
async def get_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get single price contract by ID.
    
    **Returns:**
    - Contract details
    - Analytics (total transactions, revenue, discount given)
    - Audit info (creator, approver)
    """
    contract = await PriceContractService.get_contract(
        db=db,
        contract_id=contract_id,
        user=current_user
    )
    
    return contract


# ============================================
# READ CONTRACT WITH DETAILS
# ============================================

@router.get(
    "/{contract_id}/details",
    response_model=PriceContractWithDetails,
    summary="Get contract with full details"
)
async def get_contract_details(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get contract with comprehensive details including:
    - Insurance provider information
    - Creator and approver names
    - List of applicable branches (if not all branches)
    - Count of custom pricing items
    - Validation status (is_valid_today, days_until_expiry)
    
    **Use this endpoint when:**
    - Displaying contract details page
    - Editing contract
    - Reviewing contract before approval
    """
    details = await PriceContractService.get_contract_with_details(
        db=db,
        contract_id=contract_id,
        user=current_user
    )
    
    return details


# ============================================
# UPDATE CONTRACT
# ============================================

@router.patch(
    "/{contract_id}",
    response_model=PriceContractResponse,
    summary="Update price contract"
)
async def update_contract(
    contract_id: uuid.UUID,
    update_data: PriceContractUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Update price contract (partial update).
    
    **Required Role:** Admin, Manager, or Super Admin
    
    **Restrictions:**
    - Cannot change contract_type or contract_code
    - Cannot modify pricing fields if contract has existing transactions
    - Date range must be valid (effective_to >= effective_from)
    
    **Updatable Fields:**
    - contract_name
    - description
    - discount_percentage (only if no transactions)
    - applicability rules
    - price limits
    - branch applicability
    - effective_to date
    - usage controls
    - copay amounts (only if no transactions)
    - status
    - is_active
    
    **Note:** For contracts with existing sales, create a new contract version instead of modifying pricing.
    """
    contract = await PriceContractService.update_contract(
        db=db,
        contract_id=contract_id,
        update_data=update_data,
        user=current_user
    )
    
    return contract


# ============================================
# DELETE CONTRACT
# ============================================

@router.delete(
    "/{contract_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete price contract"
)
async def delete_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin'))
):
    """
    Soft delete price contract.
    
    **Required Role:** Admin or Super Admin
    
    **Restrictions:**
    - Cannot delete default contract
    - Cannot delete contract with sales in last 30 days
    
    **Recommendation:** 
    Instead of deleting, consider suspending the contract to preserve audit trail.
    
    **Returns:**
    - Success message
    """
    result = await PriceContractService.delete_contract(
        db=db,
        contract_id=contract_id,
        user=current_user
    )
    
    return result


# ============================================
# CONTRACT ACTIONS
# ============================================

@router.post(
    "/{contract_id}/approve",
    response_model=PriceContractResponse,
    summary="Approve draft contract"
)
async def approve_contract(
    contract_id: uuid.UUID,
    request: ApproveContractRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Approve a draft contract and activate it.
    
    **Required Role:** Admin, Manager, or Super Admin
    
    **Effect:**
    - Changes status from 'draft' to 'active'
    - Sets approved_by and approved_at
    - Makes contract available for POS selection
    """
    contract = await PriceContractService.approve_contract(
        db=db,
        contract_id=contract_id,
        user=current_user,
        notes=request.notes
    )
    
    return contract


@router.post(
    "/{contract_id}/suspend",
    response_model=PriceContractResponse,
    summary="Suspend active contract"
)
async def suspend_contract(
    contract_id: uuid.UUID,
    request: SuspendContractRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Suspend an active contract.
    
    **Required Role:** Admin, Manager, or Super Admin
    
    **Effect:**
    - Changes status to 'suspended'
    - Sets is_active to False
    - Removes contract from POS selection
    - Preserves historical data
    
    **Cannot suspend:** Default contract
    """
    contract = await PriceContractService.suspend_contract(
        db=db,
        contract_id=contract_id,
        user=current_user,
        reason=request.reason
    )
    
    return contract


@router.post(
    "/{contract_id}/activate",
    response_model=PriceContractResponse,
    summary="Activate suspended contract"
)
async def activate_contract(
    contract_id: uuid.UUID,
    request: ActivateContractRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Activate a suspended or draft contract.
    
    **Required Role:** Admin, Manager, or Super Admin
    
    **Effect:**
    - Changes status to 'active'
    - Sets is_active to True
    - Makes contract available for POS selection
    
    **Validation:**
    - Contract must be within valid date range
    """
    contract = await PriceContractService.activate_contract(
        db=db,
        contract_id=contract_id,
        user=current_user
    )
    
    return contract


# ============================================
# GET AVAILABLE CONTRACTS FOR POS
# ============================================

@router.get(
    "/available/{branch_id}",
    response_model=List[dict],
    summary="Get contracts available for POS selection"
)
async def get_available_contracts(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get list of contracts available for selection at POS.
    
    **Used by:** POS frontend to populate contract dropdown
    
    **Filters contracts that are:**
    - Active
    - Valid for today's date
    - Applicable to the branch
    - User's role is allowed to use
    
    **Returns:** List of contracts with display-friendly formatting
    
    **Example Response:**
    ```json
    [
        {
            "id": "uuid",
            "code": "STANDARD",
            "name": "Standard Retail Pricing",
            "type": "standard",
            "discount_percentage": 0.00,
            "is_default": true,
            "requires_verification": false,
            "requires_approval": false,
            "display": "Standard Retail Pricing (Standard)",
            "warning": null
        },
        {
            "id": "uuid",
            "code": "GLICO-STD",
            "name": "GLICO Insurance",
            "type": "insurance",
            "discount_percentage": 10.00,
            "is_default": false,
            "requires_verification": true,
            "requires_approval": false,
            "display": "GLICO Insurance (10% + copay)",
            "warning": "Verify insurance card"
        }
    ]
    ```
    """
    contracts = await PriceContractService.get_available_contracts_for_pos(
        db=db,
        branch_id=branch_id,
        user=current_user
    )
    
    return contracts


# ============================================
# DUPLICATE CONTRACT (CREATE COPY)
# ============================================

@router.post(
    "/{contract_id}/duplicate",
    response_model=PriceContractResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate existing contract"
)
async def duplicate_contract(
    contract_id: uuid.UUID,
    new_code: str = Query(..., min_length=1, max_length=50, description="Code for new contract"),
    new_name: str = Query(..., min_length=1, max_length=255, description="Name for new contract"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role('admin', 'super_admin', 'manager'))
):
    """
    Create a copy of existing contract with new code and name.
    
    **Use case:** Creating contract versions or similar contracts for different providers
    
    **Example:** 
    - Duplicate "GLICO 2024" to create "GLICO 2025"
    - Copy "Staff Discount" to create "Intern Discount"
    """
    # Get original contract
    original = await PriceContractService.get_contract(db, contract_id, current_user)
    
    # Create new contract data
    new_contract_data = PriceContractCreate(
        organization_id=current_user.organization_id,
        contract_code=new_code,
        contract_name=new_name,
        description=original.description,
        contract_type=original.contract_type,
        is_default_contract=False,  # Never duplicate as default
        discount_type=original.discount_type,
        discount_percentage=Decimal(str(original.discount_percentage)) if original.discount_percentage else Decimal('0.00'),
        applies_to_prescription_only=original.applies_to_prescription_only,
        applies_to_otc=original.applies_to_otc,
        excluded_drug_categories=original.excluded_drug_categories,
        excluded_drug_ids=original.excluded_drug_ids,
        minimum_price_override=Decimal(str(original.minimum_price_override)) if original.minimum_price_override else None,
        maximum_discount_amount=Decimal(str(original.maximum_discount_amount)) if original.maximum_discount_amount else None,
        minimum_purchase_amount=Decimal(str(original.minimum_purchase_amount)) if original.minimum_purchase_amount else None,
        maximum_purchase_amount=Decimal(str(original.maximum_purchase_amount)) if original.maximum_purchase_amount else None,
        applies_to_all_branches=original.applies_to_all_branches,
        applicable_branch_ids=original.applicable_branch_ids,
        effective_from=date.today(),  # Start from today
        effective_to=original.effective_to,
        requires_verification=original.requires_verification,
        allowed_user_roles=original.allowed_user_roles,
        daily_usage_limit=None,
        per_customer_usage_limit=None,
        insurance_provider_id=original.insurance_provider_id,
        copay_amount=Decimal(str(original.copay_amount)) if original.copay_amount else None,
        copay_percentage=Decimal(str(original.copay_percentage)) if original.copay_percentage else None,
        status='draft',  # Always create as draft
        is_active=False
    )
    
    # Create new contract
    new_contract = await PriceContractService.create_contract(
        db=db,
        contract_data=new_contract_data,
        user=current_user
    )
    
    return new_contract


# ============================================
# HELPER: Check Contract Code Availability
# ============================================

@router.get(
    "/check-code/{contract_code}",
    response_model=dict,
    summary="Check if contract code is available"
)
async def check_contract_code(
    contract_code: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Check if contract code is available (not already used).
    
    **Use case:** Validate contract code in frontend before submission
    
    **Returns:**
    ```json
    {
        "available": true,
        "code": "NEWCODE-2024"
    }
    ```
    """
    from sqlalchemy import select
    from app.models.pricing.pricing_model import PriceContract
    
    result = await db.execute(
        select(PriceContract).where(
            PriceContract.organization_id == current_user.organization_id,
            PriceContract.contract_code == contract_code,
            PriceContract.is_deleted == False
        )
    )
    existing = result.scalar_one_or_none()
    
    return {
        "available": existing is None,
        "code": contract_code
    }