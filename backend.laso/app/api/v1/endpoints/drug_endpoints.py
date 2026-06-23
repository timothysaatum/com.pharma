"""
Drug Routes
API endpoints for drug/product management
"""
from app.schemas.base_schemas import Money
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import uuid

from app.core.deps import (
    get_db, get_current_active_user,
    require_permission
)
from app.models.user.user_model import User
from app.services.drug.drug_service import DrugService
from app.services.inventory.inventory_service import InventoryService
from app.schemas.drugs_schemas import (
    DrugCreate, DrugUpdate, DrugResponse, DrugWithInventory,
    DrugCategoryCreate, DrugCategoryResponse,
    DrugSearchFilters, BulkDrugUpdate, DrugCategoryTree,
    BulkDrugImport
)
from app.utils.pagination import Paginator, PaginationParams, PaginatedResponse


router = APIRouter(prefix="/drugs", tags=["Drugs"])


def _ensure_branch_price_access(current_user: User, branch_id: Optional[uuid.UUID]) -> None:
    if branch_id is None or current_user.is_super_admin:
        return
    if str(branch_id) not in {str(b) for b in (current_user.assigned_branches or [])}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this branch",
        )


async def _serialize_drugs_with_branch_prices(
    db: AsyncSession,
    branch_id: uuid.UUID,
    drugs: List,
) -> List[DrugResponse]:
    effective_prices = await InventoryService.get_effective_selling_prices(
        db=db,
        branch_id=branch_id,
        drugs=drugs,
    )
    responses: List[DrugResponse] = []
    for drug in drugs:
        response = DrugResponse.model_validate(drug)
        responses.append(
            response.model_copy(
                update={"unit_price": effective_prices.get(drug.id, response.unit_price)}
            )
        )
    return responses


@router.post("", response_model=DrugResponse, status_code=status.HTTP_201_CREATED)
async def create_drug(
    drug_data: DrugCreate,
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new drug
    
    **Required Permission**: manage_drugs
    
    **Validations**:
    - SKU uniqueness within organization
    - Barcode uniqueness within organization
    - Category exists (if provided)
    - Price validations
    
    **Returns**: Created drug with calculated markup percentage
    """
    # Ensure organization_id matches current user's organization
    if drug_data.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create drug for different organization"
        )
    
    drug = await DrugService.create_drug(
        db=db,
        drug_data=drug_data,
        created_by_user_id=current_user.id
    )
    
    await db.commit()
    await db.refresh(drug)
    return drug


@router.get("", response_model=PaginatedResponse[DrugResponse])
async def list_drugs(
    pagination: PaginationParams = Depends(),
    search: Optional[str] = Query(None, description="Search term"),
    category_id: Optional[uuid.UUID] = Query(None, description="Filter by category"),
    drug_type: Optional[str] = Query(None, description="Filter by drug type"),
    requires_prescription: Optional[bool] = Query(None, description="Filter by prescription requirement"),
    is_active: Optional[bool] = Query(True, description="Filter by active status"),
    min_price: Optional[Money] = Query(None, description="Minimum price"),
    max_price: Optional[Money] = Query(None, description="Maximum price"),
    manufacturer: Optional[str] = Query(None, description="Filter by manufacturer"),
    supplier: Optional[str] = Query(None, description="Filter by supplier"),
    branch_id: Optional[uuid.UUID] = Query(None, description="Return branch-effective prices for POS"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List and search drugs with pagination and filters
    
    **Filters**:
    - search: Search in name, generic_name, SKU, barcode, manufacturer
    - category_id: Filter by category
    - drug_type: prescription, otc, controlled, herbal, supplement
    - requires_prescription: Boolean filter
    - is_active: Boolean filter (default: true)
    - min_price / max_price: Price range filter
    - manufacturer: Partial match filter
    - supplier: Partial match filter
    
    **Pagination**:
    - page: Page number (default: 1)
    - page_size: Items per page (default: 50, max: 500)
    
    **Returns**: Paginated list of drugs
    """
    _ensure_branch_price_access(current_user, branch_id)
    drugs = await DrugService.search_drugs(
        db=db,
        organization_id=current_user.organization_id,
        search=search,
        category_id=category_id,
        drug_type=drug_type,
        requires_prescription=requires_prescription,
        is_active=is_active,
        min_price=min_price,
        max_price=max_price,
        manufacturer=manufacturer,
        supplier=supplier
    )
    
    # Paginate the results
    paginator = Paginator(db)
    result = paginator.paginate_list(
        items=drugs,
        params=pagination,
        schema=None if branch_id else DrugResponse
    )
    if branch_id:
        result.items = await _serialize_drugs_with_branch_prices(
            db=db,
            branch_id=branch_id,
            drugs=result.items,
        )
    
    return result


# ============================================================================
# SPECIFIC LITERAL ROUTES - Must come BEFORE parameterized routes like /{drug_id}
# ============================================================================

# Drug Category Endpoints
@router.post("/categories", response_model=DrugCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_drug_category(
    category_data: DrugCategoryCreate,
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new drug category
    
    **Required Permission**: manage_drugs
    
    **Supports**: Hierarchical categories (parent-child relationship)
    
    **Returns**: Created category with calculated path and level
    """
    if category_data.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create category for different organization"
        )
    
    category = await DrugService.create_category(db=db, category_data=category_data)
    return category


@router.get("/categories", response_model=List[DrugCategoryResponse])
async def list_drug_categories(
    parent_id: Optional[uuid.UUID] = Query(None, description="Filter by parent category"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List drug categories
    
    **Query Parameters**:
    - parent_id: Optional - Get children of specific parent (null for root categories)
    
    **Returns**: List of categories
    """
    categories = await DrugService.get_category_tree(
        db=db,
        organization_id=current_user.organization_id,
        parent_id=parent_id
    )
    
    return categories


@router.get("/categories/tree", response_model=List[DrugCategoryTree])
async def get_category_tree(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    parent_id: Optional[uuid.UUID] = None
):
    """
    Get complete category tree structure
    
    **Returns**: Hierarchical tree of all categories with nested children
    
    **Use Case**: For displaying category picker with tree structure
    """
    # This would require a recursive function to build the tree
    # For now, return root categories
    categories = await DrugService.get_category_tree(
        db=db,
        organization_id=current_user.organization_id,
        parent_id=None
    )
    
    return categories


@router.post("/bulk-update", status_code=status.HTTP_200_OK)
async def bulk_update_drugs(
    bulk_update: BulkDrugUpdate,
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Bulk update multiple drugs
    
    **Required Permission**: manage_drugs
    
    **Limits**: Maximum 100 drugs per request
    
    **Returns**: Count of successful and failed updates
    
    **Note**: Partial success possible - some drugs may update while others fail
    """
    successful, failed = await DrugService.bulk_update_drugs(
        db=db,
        organization_id=current_user.organization_id,
        bulk_update=bulk_update,
        updated_by_user_id=current_user.id
    )
    
    return {
        "successful": successful,
        "failed": failed,
        "total": len(bulk_update.drug_ids),
        "message": f"Updated {successful} drug(s) successfully, {failed} failed"
    }


@router.post("/import", status_code=status.HTTP_200_OK)
async def import_drugs(
    import_data: BulkDrugImport,
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Import drugs in bulk for pharmacy onboarding.

    **Required Permission**: manage_drugs

    **Request Body**: List of DrugCreate objects

    **Returns**: Count of successful imports and list of errors
    """
    successful, errors = await DrugService.bulk_import_drugs(
        db=db,
        organization_id=current_user.organization_id,
        drugs_to_import=import_data.drugs,
        created_by_user_id=current_user.id
    )

    return {
        "successful": successful,
        "failed": len(errors),
        "total": len(import_data.drugs),
        "errors": errors,
        "message": f"Imported {successful} drug(s) successfully, {len(errors)} failed"
    }


# Search and Filter Endpoints
@router.post("/search", response_model=PaginatedResponse[DrugResponse])
async def search_drugs_advanced(
    filters: DrugSearchFilters,
    pagination: PaginationParams = Depends(),
    branch_id: Optional[uuid.UUID] = Query(None, description="Return branch-effective prices for POS"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Advanced drug search with complex filters (POST method for complex body)
    
    **Request Body**: DrugSearchFilters with multiple filter options
    
    **Returns**: Paginated list of matching drugs
    """
    _ensure_branch_price_access(current_user, branch_id)
    drugs = await DrugService.search_drugs(
        db=db,
        organization_id=current_user.organization_id,
        **filters.model_dump(exclude_none=True)
    )
    
    paginator = Paginator(db)
    result = paginator.paginate_list(
        items=drugs,
        params=pagination,
        schema=None if branch_id else DrugResponse
    )
    if branch_id:
        result.items = await _serialize_drugs_with_branch_prices(
            db=db,
            branch_id=branch_id,
            drugs=result.items,
        )
    
    return result


# ============================================================================
# PARAMETERIZED ROUTES - Must come AFTER all specific literal routes
# ============================================================================

@router.get("/{drug_id}", response_model=DrugResponse)
async def get_drug(
    drug_id: uuid.UUID,
    branch_id: Optional[uuid.UUID] = Query(None, description="Return branch-effective price for POS"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get drug by ID
    
    **Returns**: Drug details
    
    **Errors**:
    - 404: Drug not found
    """
    _ensure_branch_price_access(current_user, branch_id)
    drug = await DrugService.get_drug_by_id(
        db=db,
        drug_id=drug_id,
        organization_id=current_user.organization_id
    )
    
    if not drug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Drug not found"
        )
    
    if branch_id:
        return (
            await _serialize_drugs_with_branch_prices(
                db=db,
                branch_id=branch_id,
                drugs=[drug],
            )
        )[0]

    return drug


@router.get("/{drug_id}/with-inventory", response_model=DrugWithInventory)
async def get_drug_with_inventory(
    drug_id: uuid.UUID,
    branch_id: Optional[uuid.UUID] = Query(None, description="Filter by specific branch"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get drug with complete inventory information
    
    **Query Parameters**:
    - branch_id: Optional - Get inventory for specific branch only
    
    **Returns**: Drug with inventory totals and status
    
    **Inventory Status**:
    - in_stock: Stock above reorder level
    - low_stock: Stock at or below reorder level but not zero
    - out_of_stock: Zero stock
    """
    _ensure_branch_price_access(current_user, branch_id)
    result = await DrugService.get_drug_with_inventory(
        db=db,
        drug_id=drug_id,
        organization_id=current_user.organization_id,
        branch_id=branch_id
    )
    
    drug_response = DrugResponse.model_validate(result["drug"])
    if branch_id:
        drug_response = (
            await _serialize_drugs_with_branch_prices(
                db=db,
                branch_id=branch_id,
                drugs=[result["drug"]],
            )
        )[0]

    return {
        **drug_response.model_dump(),
        "total_quantity": result['total_quantity'],
        "available_quantity": result['available_quantity'],
        "reserved_quantity": result['reserved_quantity'],
        "inventory_status": result['inventory_status']
    }


@router.patch("/{drug_id}", response_model=DrugResponse)
async def update_drug(
    drug_id: uuid.UUID,
    drug_data: DrugUpdate,
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Update drug information
    
    **Required Permission**: manage_drugs
    
    **Validations**:
    - SKU uniqueness (if changed)
    - Barcode uniqueness (if changed)
    - Recalculates markup if prices change
    
    **Returns**: Updated drug
    
    **Errors**:
    - 404: Drug not found
    - 400: Validation error
    """
    drug = await DrugService.update_drug(
        db=db,
        drug_id=drug_id,
        drug_data=drug_data,
        organization_id=current_user.organization_id,
        updated_by_user_id=current_user.id
    )
    
    return drug


@router.delete("/{drug_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_drug(
    drug_id: uuid.UUID,
    hard_delete: bool = Query(False, description="Permanently delete (default: soft delete)"),
    current_user: User = Depends(require_permission("manage_drugs")),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete drug (soft delete by default)
    
    **Required Permission**: manage_drugs
    
    **Query Parameters**:
    - hard_delete: true for permanent deletion, false for soft delete (default)
    
    **Validation**:
    - Cannot delete drug with existing inventory
    
    **Errors**:
    - 404: Drug not found
    - 400: Drug has inventory
    """
    await DrugService.delete_drug(
        db=db,
        drug_id=drug_id,
        organization_id=current_user.organization_id,
        deleted_by_user_id=current_user.id,
        hard_delete=hard_delete
    )
    
    return None
