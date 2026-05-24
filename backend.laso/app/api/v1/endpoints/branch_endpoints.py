"""
Branch Routes
API endpoints for branch/location management
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import uuid

from app.core.deps import (
    get_db, get_current_active_user,
    require_permission, require_role
)
from app.models.user.user_model import User
from app.schemas.branch_schemas import (
    BranchCreate, BranchUpdate, BranchResponse, BranchListItem, BranchAssignment, BranchSearchFilters
)
from app.services.branch.branch_service import BranchService
from app.utils.pagination import Paginator, PaginationParams, PaginatedResponse
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/branches", tags=["Branch Management"])

@router.post("", response_model=BranchResponse, status_code=status.HTTP_201_CREATED)
async def create_branch(
    branch_data: BranchCreate,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new branch
    
    **Required Role**: admin or super_admin
    
    **Validations**:
    - Branch code uniqueness within organization
    - Manager exists and has appropriate role (admin, manager)
    - Organization exists
    
    **Returns**: Created branch information
    
    **Note**: Only admins can create branches
    """
    try:
        # Ensure organization_id matches current user's organization
        if branch_data.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot create branch for different organization"
            )
        
        branch = await BranchService.create_branch(
            db=db,
            branch_data=branch_data,
            created_by_user_id=current_user.id
        )
        
        return branch
        
    except HTTPException:
        # Re-raise HTTPExceptions as-is
        raise
    except ValidationError as e:
        # Handle Pydantic validation errors
        logger.error(f"Validation error creating branch: {e.errors()}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors()
        )
    except ValueError as e:
        # Handle ValueError from field validators
        logger.error(f"ValueError creating branch: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        # Log and handle unexpected errors
        logger.error(f"Unexpected error creating branch: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the branch"
        )


@router.get("", response_model=PaginatedResponse[BranchListItem])
async def list_branches(
    pagination: PaginationParams = Depends(),
    search: Optional[str] = Query(None, description="Search name, code, city"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    manager_id: Optional[uuid.UUID] = Query(None, description="Filter by manager"),
    state: Optional[str] = Query(None, description="Filter by state"),
    city: Optional[str] = Query(None, description="Filter by city"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List all branches with pagination and filters
    
    **Query Paramel",ters**:
    - page: Page number (default: 1)
    - page_size: Items per page (default: 50, max: 500)
    - search: Search in name, code, city
    - is_active: Filter by active status
    - manager_id: Filter by manager
    - state: Filter by state/region
    - city: Filter by city
    
    **Returns**: Paginated list of branches
    
    **Access**: All authenticated users can view branches in their organization
    """
    branches = await BranchService.search_branches(
        db=db,
        organization_id=current_user.organization_id,
        search=search,
        is_active=is_active,
        manager_id=manager_id,
        state=state,
        city=city
    )
    
    # Paginate the results
    paginator = Paginator(db)
    result = paginator.paginate_list(
        items=branches,
        params=pagination,
        schema=BranchListItem
    )
    
    return result


@router.get("/my-branches", response_model=List[BranchListItem])
async def get_my_branches(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get branches assigned to current user
    
    **Returns**: List of branches the current user has access to
    
    **Use Case**: 
    - Populate branch selector in UI
    - Determine user's accessible branches
    - Quick access to assigned locations
    """
    if not current_user.assigned_branches:
        return []
    
    # Get branches by IDs
    from sqlalchemy import select
    from app.models.pharmacy.pharmacy_model import Branch
    
    result = await db.execute(
        select(Branch).where(
            Branch.id.in_(current_user.assigned_branches),
            Branch.is_deleted == False
        ).order_by(Branch.name)
    )
    branches = result.scalars().all()
    
    return [BranchListItem.model_validate(b) for b in branches]


@router.get("/{branch_id}", response_model=BranchResponse)
async def get_branch(
    branch_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get branch by ID
    
    **Returns**: Branch details
    
    **Errors**:
    - 404: Branch not found
    - 403: No access to this branch
    """
    branch = await BranchService.get_branch_by_id(
        db=db,
        branch_id=branch_id,
        organization_id=current_user.organization_id
    )
    
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found"
        )
    
    # Check if user has access (unless admin/super_admin)
    if current_user.role not in ['admin', 'super_admin']:
        assigned_branch_ids = {str(b) for b in (current_user.assigned_branches or [])}
        if str(branch_id) not in assigned_branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this branch"
            )
    
    return branch


@router.get("/code/{code}", response_model=BranchResponse)
async def get_branch_by_code(
    code: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get branch by unique code
    
    **Use Case**: Quick lookup by branch code
    
    **Returns**: Branch details
    
    **Errors**:
    - 404: Branch not found
    """
    branch = await BranchService.get_branch_by_code(
        db=db,
        code=code,
        organization_id=current_user.organization_id
    )
    
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch with code '{code}' not found"
        )
    
    return branch


@router.patch("/{branch_id}", response_model=BranchResponse)
async def update_branch(
    branch_id: uuid.UUID,
    branch_data: BranchUpdate,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Update branch information
    
    **Required Role**: admin or super_admin
    
    **Validations**:
    - Branch code uniqueness (if changed)
    - Manager exists and has appropriate role (if changed)
    
    **Returns**: Updated branch
    
    **Errors**:
    - 404: Branch not found
    - 400: Validation error
    
    **Note**: Only admins can update branches
    """
    branch = await BranchService.update_branch(
        db=db,
        branch_id=branch_id,
        branch_data=branch_data,
        organization_id=current_user.organization_id,
        updated_by_user_id=current_user.id
    )
    
    return branch


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_branch(
    branch_id: uuid.UUID,
    hard_delete: bool = Query(False, description="Permanently delete (default: soft delete)"),
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete branch (soft delete by default)
    
    **Required Role**: admin or super_admin
    
    **Query Parameters**:
    - hard_delete: true for permanent deletion, false for soft delete (default)
    
    **Validations**:
    - Cannot delete branch with existing inventory
    - Cannot soft delete branch with recent sales (this month)
    
    **Errors**:
    - 404: Branch not found
    - 400: Branch has inventory or recent sales
    
    **Note**: Only admins can delete branches. Use with caution!
    """
    await BranchService.delete_branch(
        db=db,
        branch_id=branch_id,
        organization_id=current_user.organization_id,
        deleted_by_user_id=current_user.id,
        hard_delete=hard_delete
    )
    
    return None


@router.post("/{branch_id}/activate", response_model=BranchResponse)
async def activate_branch(
    branch_id: uuid.UUID,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Activate a deactivated branch
    
    **Required Role**: admin or super_admin
    
    **Returns**: Activated branch
    
    **Use Case**: Re-enable a temporarily closed branch
    """
    from app.schemas.branch_schemas import BranchUpdate
    
    branch = await BranchService.update_branch(
        db=db,
        branch_id=branch_id,
        branch_data=BranchUpdate(is_active=True), # type: ignore
        organization_id=current_user.organization_id,
        updated_by_user_id=current_user.id
    )
    
    return branch


@router.post("/{branch_id}/deactivate", response_model=BranchResponse)
async def deactivate_branch(
    branch_id: uuid.UUID,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Deactivate a branch (without deleting)
    
    **Required Role**: admin or super_admin
    
    **Returns**: Deactivated branch
    
    **Use Case**: Temporarily close a branch (e.g., for renovation)
    
    **Note**: Deactivated branches can still be viewed but not used for new transactions
    """
    from app.schemas.branch_schemas import BranchUpdate
    
    branch = await BranchService.update_branch(
        db=db,
        branch_id=branch_id,
        branch_data=BranchUpdate(is_active=False), # type: ignore
        organization_id=current_user.organization_id,
        updated_by_user_id=current_user.id
    )
    
    return branch


# User Assignment Endpoints

@router.post("/assign-user", status_code=status.HTTP_200_OK)
async def assign_user_to_branches(
    assignment: BranchAssignment,
    current_user: User = Depends(require_role("admin", "super_admin")),
    db: AsyncSession = Depends(get_db)
):
    """
    Assign user to multiple branches
    
    **Required Role**: admin or super_admin
    
    **Request Body**:
    - user_id: User to assign
    - branch_ids: List of branch IDs
    
    **Returns**: Updated user with branch assignments
    
    **Use Case**: 
    - Grant user access to specific branches
    - Update user's accessible locations
    - Multi-branch staff management
    
    **Note**: This replaces existing branch assignments
    """
    user = await BranchService.assign_user_to_branches(
        db=db,
        user_id=assignment.user_id,
        branch_ids=assignment.branch_ids,
        organization_id=current_user.organization_id
    )
    
    return {
        "success": True,
        "message": f"User assigned to {len(assignment.branch_ids)} branch(es)",
        "user_id": str(user.id),
        "assigned_branches": [str(b) for b in user.assigned_branches]
    }


@router.get("/{branch_id}/users", response_model=List[dict])
async def get_branch_users(
    branch_id: uuid.UUID,
    current_user: User = Depends(require_permission("view_reports")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all users assigned to a branch
    **Required Permission**: view_reports
    **Returns**: List of users with access to this branch
    **Use Case**: 
    - View staff at a branch
    - Audit user access
    - Branch staffing reports
    """
    from sqlalchemy import select
    from app.models.user.user_model import User as UserModel
    
    # Verify branch exists and user has access
    branch = await BranchService.get_branch_by_id(
        db=db,
        branch_id=branch_id,
        organization_id=current_user.organization_id
    )
    
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found"
        )
    
    # Get all active users in the organization
    result = await db.execute(
        select(UserModel).where(
            UserModel.organization_id == current_user.organization_id,
            UserModel.is_deleted == False
        ).order_by(UserModel.full_name)
    )
    
    all_users = result.scalars().all()
    
    branch_users = [
        user for user in all_users
        if user.has_branch_access(branch_id)
    ]
    
    return [
        {
            "id": str(user.id),
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active
        }
        for user in branch_users
    ]


# Search Endpoint

@router.post("/search", response_model=PaginatedResponse[BranchListItem])
async def search_branches_advanced(
    filters: BranchSearchFilters,
    pagination: PaginationParams = Depends(),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Advanced branch search with complex filters (POST method for complex body)
    
    **Request Body**: BranchSearchFilters with multiple filter options
    
    **Returns**: Paginated list of matching branches
    """
    branches = await BranchService.search_branches(
        db=db,
        organization_id=current_user.organization_id,
        **filters.model_dump(exclude_none=True)
    )
    
    paginator = Paginator(db)
    result = paginator.paginate_list(
        items=branches,
        params=pagination,
        schema=BranchListItem
    )
    
    return result
