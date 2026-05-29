"""
Insurance Provider API Endpoints

Provides comprehensive endpoints for managing insurance providers:
- Create new providers
- List/search providers
- Get provider details
- Update provider information
- Activate/deactivate providers
- Delete providers

Access: admin | super_admin only (enforced via require_role)
"""

from fastapi import APIRouter, Depends, Query, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.core.deps import get_db, get_current_user, require_role
from app.models.user.user_model import User
from app.services.insurance import InsuranceProviderService
from app.schemas.insurance_provider_schemas import (
    InsuranceProviderCreate,
    InsuranceProviderUpdate,
    InsuranceProviderResponse,
    InsuranceProviderListResponse,
    InsuranceProviderSearchItem,
)

router = APIRouter(prefix="/insurance-providers", tags=["insurance-providers"])


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=InsuranceProviderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("admin", "super_admin"))],
)
async def create_insurance_provider(
    data: InsuranceProviderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new insurance provider.
    
    **Permissions:** admin | super_admin
    
    **Validation:**
    - Provider code must be unique per organization
    - Code must be alphanumeric + underscores only
    
    **Example:**
    ```json
    {
      "name": "NHIS Ghana",
      "code": "NHIS_GH",
      "email": "support@nhis.gov.gh",
      "phone": "+233-302-670670",
      "is_active": true,
      "requires_preauth": false,
      "billing_cycle": "monthly",
      "payment_terms": "NET30"
    }
    ```
    """
    return await InsuranceProviderService.create(db, data, current_user)


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=InsuranceProviderListResponse,
)
async def list_insurance_providers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    active_only: bool = Query(False),
):
    """
    List insurance providers with pagination.
    
    **Permissions:** All authenticated users
    
    **Query Parameters:**
    - `skip`: Number of records to skip (default: 0)
    - `limit`: Records per page (default: 50, max: 100)
    - `active_only`: Show only active providers (default: false)
    """
    providers, total = await InsuranceProviderService.list(
        db,
        current_user.organization_id,
        skip=skip,
        limit=limit,
        active_only=active_only,
    )
    
    return InsuranceProviderListResponse(
        providers=providers,
        total=total,
        page=skip // limit + 1,
        page_size=limit,
    )


@router.get(
    "/search",
    response_model=list[InsuranceProviderSearchItem],
)
async def search_insurance_providers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: str = Query("", description="Search by name or code"),
    active_only: bool = Query(True),
):
    """
    Search insurance providers by name or code.
    
    **Permissions:** All authenticated users
    
    **Use Case:** Dropdown/autocomplete in forms (e.g., contract creation)
    
    **Query Parameters:**
    - `q`: Search query (searches name and code)
    - `active_only`: Show only active providers (default: true)
    """
    return await InsuranceProviderService.search(
        db,
        current_user.organization_id,
        query=q,
        active_only=active_only,
    )


@router.get(
    "/{provider_id}",
    response_model=InsuranceProviderResponse,
)
async def get_insurance_provider(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get insurance provider details by ID.
    
    **Permissions:** All authenticated users
    """
    return await InsuranceProviderService.get_by_id(
        db,
        provider_id,
        current_user.organization_id,
    )


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

@router.patch(
    "/{provider_id}",
    response_model=InsuranceProviderResponse,
    dependencies=[Depends(require_role("admin", "super_admin"))],
)
async def update_insurance_provider(
    provider_id: uuid.UUID,
    data: InsuranceProviderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update insurance provider details.
    
    **Permissions:** admin | super_admin
    
    **Restrictions:**
    - Cannot change provider code
    - Only partial updates allowed (send only fields to change)
    """
    return await InsuranceProviderService.update(
        db,
        provider_id,
        data,
        current_user.organization_id,
    )


# ─────────────────────────────────────────────────────────────
# STATUS MANAGEMENT
# ─────────────────────────────────────────────────────────────

@router.post(
    "/{provider_id}/deactivate",
    response_model=InsuranceProviderResponse,
    dependencies=[Depends(require_role("admin", "super_admin"))],
)
async def deactivate_insurance_provider(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Deactivate an insurance provider.
    
    **Permissions:** admin | super_admin
    
    **Validation:**
    - Cannot deactivate if active contracts still use this provider
    
    **Effect:** Provider can no longer be selected in new contracts
    """
    return await InsuranceProviderService.deactivate(
        db,
        provider_id,
        current_user.organization_id,
    )


@router.post(
    "/{provider_id}/activate",
    response_model=InsuranceProviderResponse,
    dependencies=[Depends(require_role("admin", "super_admin"))],
)
async def activate_insurance_provider(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Activate a deactivated insurance provider.
    
    **Permissions:** admin | super_admin
    """
    data = InsuranceProviderUpdate(is_active=True)
    return await InsuranceProviderService.update(
        db,
        provider_id,
        data,
        current_user.organization_id,
    )


# ─────────────────────────────────────────────────────────────
# DELETE
# ─────────────────────────────────────────────────────────────

@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("admin", "super_admin"))],
)
async def delete_insurance_provider(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete (soft delete) an insurance provider.
    
    **Permissions:** admin | super_admin
    
    **Validation:**
    - Cannot delete if any contracts reference this provider
    
    **Effect:** Provider is soft-deleted and hidden from queries
    """
    await InsuranceProviderService.delete(
        db,
        provider_id,
        current_user.organization_id,
    )
