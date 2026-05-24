"""
Customer Routes
===============
API endpoints for customer management — CRUD, quick POS search,
and loyalty point management.

Route ordering rule (matching other files in this project):
  All specific literal sub-paths (/search, /stats) MUST be declared
  BEFORE the parameterized route (/{customer_id}) so FastAPI does not
  swallow them as UUID path parameters.

Permission map:
  view_customers        — list, get, search           (all authenticated roles)
  manage_customers      — create, update              (pharmacist, manager, admin, super_admin)
  manage_loyalty        — award/deduct loyalty points (manager, admin, super_admin)
  delete_customers      — soft-delete                 (admin, super_admin)
"""

import logging
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_db,
    get_current_active_user,
    require_permission,
    require_role,
)
from app.models.user.user_model import User
from app.services.customer.customer_service import CustomerService
from app.schemas.customer_schemas import (
    CustomerCreate,
    CustomerUpdate,
    CustomerResponse,
    CustomerWithDetails,
    CustomerListResponse,
    CustomerSearchResult,
    AwardLoyaltyPointsRequest,
    DeductLoyaltyPointsRequest,
)
from app.utils.pagination import PaginationParams

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["Customers"])


# ============================================================================
# CREATE
# ============================================================================

@router.post(
    "",
    response_model=CustomerWithDetails,
    status_code=status.HTTP_201_CREATED,
)
async def create_customer(
    customer_data: CustomerCreate,
    current_user: User = Depends(require_permission("manage_customers")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new customer.

    **Required Permission**: manage_customers

    **Customer types and their requirements**:
    - `walk_in`: all fields optional — fast registration at POS
    - `registered`: first_name + last_name + (phone or email) required
    - `insurance`: first_name + last_name + insurance_provider_id + insurance_member_id required
    - `corporate`: first_name + last_name + preferred_contract_id required

    **Validations**:
    - Phone uniqueness within organisation (non-walk_in types)
    - Email uniqueness within organisation (if provided)
    - Insurance provider must exist
    - Preferred contract must exist

    **Returns**: Full customer profile with resolved relationship names
    """
    # Organisation isolation — customer must belong to the caller's org
    if customer_data.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create customer for a different organisation",
        )

    return await CustomerService.create_customer(
        db=db,
        customer_data=customer_data,
        created_by_user_id=current_user.id,
    )


# ============================================================================
# LIST (paginated + filtered)
# ============================================================================

@router.get(
    "",
    response_model=CustomerListResponse,
)
async def list_customers(
    pagination: PaginationParams = Depends(),
    search: Optional[str] = Query(
        None,
        min_length=1,
        max_length=100,
        description="Search in name, phone, email, member ID",
    ),
    customer_type: Optional[str] = Query(
        None,
        pattern="^(walk_in|registered|insurance|corporate)$",
        description="Filter by customer type",
    ),
    loyalty_tier: Optional[str] = Query(
        None,
        pattern="^(bronze|silver|gold|platinum)$",
        description="Filter by loyalty tier",
    ),
    insurance_provider_id: Optional[uuid.UUID] = Query(
        None,
        description="Filter by insurance provider",
    ),
    preferred_contract_id: Optional[uuid.UUID] = Query(
        None,
        description="Filter by preferred contract",
    ),
    is_active: Optional[bool] = Query(
        None,
        description="Filter by active status (default: all)",
    ),
    min_loyalty_points: Optional[int] = Query(
        None,
        ge=0,
        description="Minimum loyalty points filter",
    ),
    sort_by: Optional[str] = Query(
        default="created_at",
        pattern="^(created_at|first_name|last_name|loyalty_points)$",
        description="Sort field",
    ),
    sort_order: Optional[str] = Query(
        default="desc",
        pattern="^(asc|desc)$",
        description="Sort direction",
    ),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List customers with pagination and filters.

    **Filters**:
    - search: partial match on name, phone, email, insurance member ID
    - customer_type: walk_in | registered | insurance | corporate
    - loyalty_tier: bronze | silver | gold | platinum
    - insurance_provider_id: filter to customers of a specific insurer
    - preferred_contract_id: filter to customers on a specific contract
    - is_active: true | false (default: return all)
    - min_loyalty_points: minimum points threshold

    **Sorting**:
    - sort_by: created_at | first_name | last_name | loyalty_points
    - sort_order: asc | desc (default: desc)

    **Pagination**:
    - page: page number (default: 1)
    - page_size: items per page (default: 25, max: 100)

    **Returns**: Paginated customer list with totals
    """
    customers, total = await CustomerService.list_customers(
        db=db,
        organization_id=current_user.organization_id,
        search=search,
        customer_type=customer_type,
        loyalty_tier=loyalty_tier,
        insurance_provider_id=insurance_provider_id,
        preferred_contract_id=preferred_contract_id,
        is_active=is_active,
        min_loyalty_points=min_loyalty_points,
        sort_by=sort_by or "created_at",
        sort_order=sort_order or "desc",
        page=pagination.page,
        page_size=pagination.page_size,
    )

    total_pages = (total + pagination.page_size - 1) // pagination.page_size

    return CustomerListResponse(
        customers=[CustomerResponse.model_validate(c) for c in customers],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        total_pages=total_pages,
    )


# ============================================================================
# SPECIFIC LITERAL SUB-ROUTES
# MUST come BEFORE /{customer_id} to avoid FastAPI treating them as UUIDs
# ============================================================================

@router.get(
    "/search",
    response_model=CustomerSearchResult,
)
async def search_customers_quick(
    q: str = Query(
        ...,
        min_length=2,
        max_length=100,
        description="Search term — name, phone, email, or member ID",
    ),
    limit: int = Query(
        default=10,
        ge=1,
        le=50,
        description="Maximum results to return (default: 10, max: 50)",
    ),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Quick customer lookup for the POS typeahead.

    Returns a minimal subset of customer data optimised for fast display:
    full name, phone, email, loyalty tier, insurance flag, contract name.

    **Use case**: POS cashier searches while building a cart.

    **Query Parameters**:
    - q: search string (min 2 chars) — matched against name, phone, email, member ID
    - limit: max results to return (1–50, default 10)

    **Returns**: CustomerSearchResult with matches list and search_term echo
    """
    return await CustomerService.search_customers_quick(
        db=db,
        organization_id=current_user.organization_id,
        query=q,
        limit=limit,
    )


# ============================================================================
# PARAMETERIZED ROUTES — must come AFTER all literal sub-paths above
# ============================================================================

@router.get(
    "/{customer_id}",
    response_model=CustomerWithDetails,
)
async def get_customer(
    customer_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get a single customer by ID with full details.

    **Returns**: Customer profile with:
    - Insurance provider name and code (if applicable)
    - Preferred contract name and discount percentage (if applicable)
    - Purchase statistics: total purchases, total spent, last purchase date

    **Errors**:
    - 404: Customer not found or belongs to a different organisation
    """
    return await CustomerService.get_customer_by_id(
        db=db,
        customer_id=customer_id,
        organization_id=current_user.organization_id,
    )


@router.patch(
    "/{customer_id}",
    response_model=CustomerWithDetails,
)
async def update_customer(
    customer_id: uuid.UUID,
    update_data: CustomerUpdate,
    current_user: User = Depends(require_permission("manage_customers")),
    db: AsyncSession = Depends(get_db),
):
    """
    Partially update a customer.

    **Required Permission**: manage_customers

    Only fields present in the request body are updated.
    Fields omitted from the body retain their current values.

    **Validations**:
    - Phone uniqueness (if phone is changing)
    - Email uniqueness (if email is changing)
    - Insurance provider exists (if updating insurance fields)
    - Preferred contract exists (if updating contract preference)

    **Returns**: Updated customer with full details

    **Errors**:
    - 404: Customer not found
    - 409: Phone or email already in use by another customer
    """
    return await CustomerService.update_customer(
        db=db,
        customer_id=customer_id,
        organization_id=current_user.organization_id,
        update_data=update_data,
        updated_by_user_id=current_user.id,
    )


@router.delete(
    "/{customer_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_customer(
    customer_id: uuid.UUID,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Soft-delete a customer.

    **Required Role**: admin or super_admin

    Sets `is_deleted=True` and `deleted_at`. Customer data is preserved
    for audit and financial history purposes.

    **Guard**: Cannot delete a customer with sales in the last 90 days.
    Deactivate instead (via PATCH) to preserve financial records.

    **Returns**: Confirmation message with deleted customer ID

    **Errors**:
    - 404: Customer not found
    - 400: Customer has recent sales — deactivate instead
    """
    return await CustomerService.delete_customer(
        db=db,
        customer_id=customer_id,
        organization_id=current_user.organization_id,
        deleted_by_user_id=current_user.id,
    )


# ============================================================================
# LOYALTY POINT MANAGEMENT
# Sub-routes under /{customer_id}/loyalty/
# ============================================================================

@router.post(
    "/{customer_id}/loyalty/award",
    response_model=CustomerWithDetails,
)
async def award_loyalty_points(
    customer_id: uuid.UUID,
    request: AwardLoyaltyPointsRequest,
    current_user: User = Depends(require_role("manager", "admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually award loyalty points to a customer.

    **Required Role**: manager, admin, or super_admin

    Points are awarded on top of the customer's current balance.
    Loyalty tier is automatically recalculated after the award.

    **Tier thresholds**:
    - Bronze:   0 – 499 points
    - Silver:   500 – 1 999 points
    - Gold:     2 000 – 4 999 points
    - Platinum: 5 000+ points

    **Validation**:
    - Customer must be active
    - Customer must not be walk_in type (register first)

    **Returns**: Updated customer with new points total and tier

    **Errors**:
    - 404: Customer not found
    - 400: Customer is walk_in or inactive
    """
    return await CustomerService.award_loyalty_points(
        db=db,
        customer_id=customer_id,
        organization_id=current_user.organization_id,
        points=request.points,
        reason=request.reason,
        awarded_by_user_id=current_user.id,
    )


@router.post(
    "/{customer_id}/loyalty/deduct",
    response_model=CustomerWithDetails,
)
async def deduct_loyalty_points(
    customer_id: uuid.UUID,
    request: DeductLoyaltyPointsRequest,
    current_user: User = Depends(require_role("manager", "admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually deduct loyalty points from a customer.

    **Required Role**: manager, admin, or super_admin

    Points cannot go below 0. Loyalty tier is recalculated after deduction.

    **Returns**: Updated customer with new points total and tier

    **Errors**:
    - 404: Customer not found
    - 400: Insufficient points to deduct the requested amount
    """
    return await CustomerService.deduct_loyalty_points(
        db=db,
        customer_id=customer_id,
        organization_id=current_user.organization_id,
        points=request.points,
        reason=request.reason,
        deducted_by_user_id=current_user.id,
    )