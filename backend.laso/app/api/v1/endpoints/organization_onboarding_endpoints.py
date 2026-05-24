"""
Organization Onboarding API Endpoints
Secure endpoints for managing organization onboarding.
Requires super_admin role for most operations.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, desc
from typing import Optional
import uuid

from app.db.dependencies import get_db
from app.core.deps import (
    get_current_user,
    require_role
)
from app.models.user.user_model import User
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.services.org.organization_onboarding_service import OrganizationOnboardingService
from app.schemas.organization_onboarding_schemas import (
    OrganizationOnboardingRequest,
    OrganizationOnboardingResponse,
    SubscriptionUpdateRequest,
    OrganizationActivationRequest,
    OrganizationStatsResponse,
    OrganizationSettingsUpdate
)
from app.schemas.organization import OrganizationResponse, OrganizationUpdate
from app.utils.pagination import PaginatedResponse, Paginator, PaginationParams


router = APIRouter(prefix="/organizations", tags=["Organization Onboarding"])


@router.post(
    "/onboard",
    response_model=OrganizationOnboardingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Onboard new organization",
    description="""
    Complete organization onboarding process. Creates:
    - Organization
    - Admin user
    - Branches (default or custom list)
    - Initial settings
    
    **Requires super_admin role**
    """,
    dependencies=[Depends(require_role("super_admin"))]
)
async def onboard_organization(
    request: Request,
    onboarding_data: OrganizationOnboardingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Onboard a new organization with complete setup
    
    - **name**: Unique organization name
    - **type**: Type (small_shop, pharmacy, hospital_pharmacy, chain)
    - **admin**: Admin user credentials
    - **subscription_tier**: basic, professional, or enterprise
    - **branches**: Optional list of branches to create (max 10). If not provided, one default branch is created.
    
    Returns complete organization details with admin credentials and created branches
    """
    service = OrganizationOnboardingService(db)
    
    # Prepare organization data
    org_data = {
        "name": onboarding_data.name,
        "type": onboarding_data.type,
        "license_number": onboarding_data.license_number,
        "tax_id": onboarding_data.tax_id,
        "phone": onboarding_data.phone,
        "email": onboarding_data.email,
        "address": onboarding_data.address,
        "subscription_tier": onboarding_data.subscription_tier,
        "currency": onboarding_data.currency,
        "timezone": onboarding_data.timezone,
        "settings": onboarding_data.additional_settings or {}
    }
    
    # Prepare admin data
    admin_data = {
        "username": onboarding_data.admin.username,
        "email": onboarding_data.admin.email,
        "full_name": onboarding_data.admin.full_name,
        "password": onboarding_data.admin.password,
        "phone": onboarding_data.admin.phone,
        "employee_id": onboarding_data.admin.employee_id,
        "role": "admin"  # Admin role for the organization
    }
    
    # Prepare branches data
    branches_data = None
    if onboarding_data.branches:
        branches_data = [
            {
                "name": branch.name,
                "phone": branch.phone,
                "email": branch.email,
                "address": branch.address,
                "operating_hours": branch.operating_hours
            }
            for branch in onboarding_data.branches
        ]
    
    # Create organization
    result = await service.create_organization_with_admin(
        org_data=org_data,
        admin_data=admin_data,
        branches_data=branches_data,
        created_by=current_user.id
    )
    
    # Return response with temporary credentials
    return OrganizationOnboardingResponse(
        organization=result["organization"],
        admin_user=result["admin_user"],
        branches=result["branches"],
        message=result["message"],
        temp_credentials={
            "username": onboarding_data.admin.username,
            "note": "Please change password on first login"
        }
    )


@router.get(
    "",
    response_model=PaginatedResponse[OrganizationResponse],
    summary="List all organizations",
    description="Get paginated list of all organizations. **Requires super_admin or admin role**",
    dependencies=[Depends(require_role("super_admin", "admin"))]
)
async def list_organizations(
    pagination: PaginationParams = Depends(),
    search: Optional[str] = Query(None, description="Search by name"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    subscription_tier: Optional[str] = Query(
        None,
        pattern="^(basic|professional|enterprise)$",
        description="Filter by subscription tier"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all organizations with pagination and filtering
    
    - **page**: Page number (default: 1)
    - **page_size**: Items per page (default: 50, max: 500)
    - **search**: Search organizations by name
    - **is_active**: Filter by active status
    - **subscription_tier**: Filter by subscription tier
    """
    # Build query
    query = select(Organization)

    if current_user.role == "admin":
        # Admins can only see their own organization
        query = query.where(Organization.id == current_user.organization_id)
    
    # Apply filters
    filters = []
    if search:
        filters.append(Organization.name.ilike(f"%{search}%"))
    if is_active is not None:
        filters.append(Organization.is_active == is_active)
    if subscription_tier:
        filters.append(Organization.subscription_tier == subscription_tier)
    
    if filters:
        query = query.where(and_(*filters))
    
    # Add ordering
    query = query.order_by(desc(Organization.created_at))
    
    # Paginate
    paginator = Paginator(db)
    return await paginator.paginate(
        query=query,
        params=pagination,
        schema=OrganizationResponse
    )


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Get organization details",
    description="Get detailed information about a specific organization"
)
async def get_organization(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get organization by ID
    
    Users can only view their own organization unless they are super_admin
    """
    # Check authorization
    if current_user.role != "super_admin":
        if current_user.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this organization"
            )
    
    # Get organization
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    organization = result.scalar_one_or_none()
    
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    return organization


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Update organization",
    description="Update organization details. **Requires admin or super_admin role**",
    dependencies=[Depends(require_role("admin", "super_admin"))]
)
async def update_organization(
    organization_id: uuid.UUID,
    update_data: OrganizationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update organization information
    
    Admins can only update their own organization
    Super admins can update any organization
    """
    # Check authorization
    if current_user.role != "super_admin":
        if current_user.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this organization"
            )
    
    # Get organization
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    organization = result.scalar_one_or_none()
    
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(organization, field, value)
    
    await db.commit()
    await db.refresh(organization)
    
    return organization


@router.post(
    "/{organization_id}/activate",
    response_model=OrganizationResponse,
    summary="Activate organization",
    description="Activate an inactive organization. **Requires super_admin role**",
    dependencies=[Depends(require_role("super_admin"))]
)
async def activate_organization(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Activate an organization"""
    service = OrganizationOnboardingService(db)
    organization = await service.activate_organization(
        organization_id=organization_id,
        activated_by=current_user.id
    )
    return organization


@router.post(
    "/{organization_id}/deactivate",
    response_model=OrganizationResponse,
    summary="Deactivate organization",
    description="Deactivate an active organization. **Requires super_admin role**",
    dependencies=[Depends(require_role("super_admin"))]
)
async def deactivate_organization(
    organization_id: uuid.UUID,
    request_data: OrganizationActivationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Deactivate an organization"""
    service = OrganizationOnboardingService(db)
    organization = await service.deactivate_organization(
        organization_id=organization_id,
        deactivated_by=current_user.id,
        reason=request_data.reason
    )
    return organization


@router.post(
    "/{organization_id}/subscription",
    response_model=OrganizationResponse,
    summary="Update subscription",
    description="Update organization subscription tier and duration. **Requires super_admin role**",
    dependencies=[Depends(require_role("super_admin"))]
)
async def update_subscription(
    organization_id: uuid.UUID,
    subscription_data: SubscriptionUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update organization subscription
    
    - **subscription_tier**: New tier (basic, professional, enterprise)
    - **extend_months**: Number of months to extend (1-60)
    """
    service = OrganizationOnboardingService(db)
    organization = await service.update_subscription(
        organization_id=organization_id,
        subscription_tier=subscription_data.subscription_tier,
        extend_months=subscription_data.extend_months,
        updated_by=current_user.id
    )
    return organization


@router.patch(
    "/{organization_id}/settings",
    response_model=OrganizationResponse,
    summary="Update organization settings",
    description="Update organization-specific settings. **Requires admin or super_admin role**",
    dependencies=[Depends(require_role("admin", "super_admin"))]
)
async def update_organization_settings(
    organization_id: uuid.UUID,
    settings_data: OrganizationSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update organization settings

    - **currency**: Currency code
    - **timezone**: Timezone
    - **low_stock_threshold**: Low stock alert threshold
    - **enable_loyalty_program**: Enable/disable loyalty program
    - And more...
    """
    # Check authorization
    if current_user.role != "super_admin":
        if current_user.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this organization"
            )

    # Get organization
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    organization = result.scalar_one_or_none()

    if not organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )

    # Update settings
    current_settings = organization.settings or {}
    update_dict = settings_data.model_dump(exclude_unset=True)

    # Merge new settings with existing
    current_settings.update(update_dict)
    organization.settings = current_settings

    await db.commit()
    await db.refresh(organization)

    return organization


@router.get(
    "/{organization_id}/stats",
    response_model=OrganizationStatsResponse,
    summary="Get organization statistics",
    description="Get comprehensive statistics for an organization"
)
async def get_organization_stats(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get organization statistics including:
    - Total branches, users, drugs, customers
    - Sales metrics
    - Subscription status
    """
    # Check authorization
    if current_user.role != "super_admin":
        if current_user.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this organization"
            )
    
    # Get organization
    result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    organization = result.scalar_one_or_none()
    
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    # Get counts
    from app.models.inventory.inventory_model import Drug
    from app.models.customer.customer_model import Customer
    from app.models.sales.sales_model import Sale
    from datetime import date, timedelta
    
    # Count branches
    result = await db.execute(
        select(func.count(Branch.id)).where(
            Branch.organization_id == organization_id
        )
    )
    total_branches = result.scalar() or 0
    
    # Count users
    result = await db.execute(
        select(func.count(User.id)).where(
            and_(
                User.organization_id == organization_id,
                User.deleted_at.is_(None)
            )
        )
    )
    total_users = result.scalar() or 0
    
    # Count drugs
    result = await db.execute(
        select(func.count(Drug.id)).where(
            and_(
                Drug.organization_id == organization_id,
                Drug.deleted_at.is_(None)
            )
        )
    )
    total_drugs = result.scalar() or 0
    
    # Count customers
    result = await db.execute(
        select(func.count(Customer.id)).where(
            and_(
                Customer.organization_id == organization_id,
                Customer.deleted_at.is_(None)
            )
        )
    )
    total_customers = result.scalar() or 0
    
    # Count today's sales
    result = await db.execute(
        select(func.count(Sale.id)).where(
            and_(
                Sale.organization_id == organization_id,
                func.date(Sale.created_at) == date.today()
            )
        )
    )
    total_sales_today = result.scalar() or 0
    
    # Count this month's sales
    first_day_of_month = date.today().replace(day=1)
    result = await db.execute(
        select(func.count(Sale.id)).where(
            and_(
                Sale.organization_id == organization_id,
                func.date(Sale.created_at) >= first_day_of_month
            )
        )
    )
    total_sales_this_month = result.scalar() or 0
    
    # Calculate subscription status
    from datetime import datetime, timezone
    
    subscription_status = "active"
    days_until_expiry = None
    
    if organization.subscription_expires_at:
        if organization.subscription_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            subscription_status = "expired"
            days_until_expiry = 0
        else:
            delta = organization.subscription_expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            days_until_expiry = delta.days
            
            if days_until_expiry <= 7:
                subscription_status = "expiring_soon"
            elif days_until_expiry <= 30:
                subscription_status = "active_expiring_soon"
    
    return OrganizationStatsResponse(
        organization_id=organization.id,
        total_branches=total_branches,
        total_users=total_users,
        total_drugs=total_drugs,
        total_customers=total_customers,
        total_sales_today=total_sales_today,
        total_sales_this_month=total_sales_this_month,
        subscription_status=subscription_status,
        subscription_expires_at=organization.subscription_expires_at,
        days_until_expiry=days_until_expiry,
        is_active=organization.is_active
    )