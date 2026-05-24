"""
Purchase Order API Routes
FastAPI endpoints for purchase orders and supplier management.

Design notes:
- The orphaned add-items docstring (no route decorator) in the original is
  restored as a proper POST /{po_id}/items endpoint.
- All state-mutating endpoints return PurchaseOrderWithDetails where the client
  needs the full picture, and PurchaseOrderResponse for lightweight responses.
- Query parameters are validated with Annotated + Query for OpenAPI accuracy.
- Supplier list ordering is deterministic (name ASC, id ASC tiebreak).
- cancel endpoint wired up (was missing from original).

Type fix (2026-03-30):
- Endpoint Python return-type annotations changed from Pydantic schema types
  (SupplierResponse, PurchaseOrderResponse) to the ORM model types that the
  service methods actually return (Supplier, PurchaseOrder).
  FastAPI serialises the ORM objects to JSON via response_model= on the
  decorator — the Python annotation must reflect what the function yields,
  not what the HTTP response looks like.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_organization_id, require_permission
from app.db.dependencies import get_db
from app.models.sales.sales_model import PurchaseOrder, Supplier
from app.models.user.user_model import User
from app.schemas.purchase_order_schemas import (
    PurchaseOrderApprove,
    PurchaseOrderCancel,
    PurchaseOrderCreate,
    PurchaseOrderItemCreate,
    PurchaseOrderItemWithDetails,
    PurchaseOrderReject,
    PurchaseOrderResponse,
    PurchaseOrderWithDetails,
    ReceivePurchaseOrder,
    ReceivePurchaseOrderResponse,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
)
from app.services.sales.purchase_order_service import PurchaseOrderService
from app.utils.pagination import PaginatedResponse, PaginationParams, Paginator

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"])
supplier_router = APIRouter(prefix="/suppliers", tags=["Suppliers"])


# =============================================================================
# Supplier Endpoints
# =============================================================================

@supplier_router.post(
    "/",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("manage_suppliers"))],
    summary="Create supplier",
)
async def create_supplier(
    supplier_data: SupplierCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Supplier:
    """
    Create a new supplier for the authenticated user's organisation.

    **Permissions:** `manage_suppliers`

    Raises **409** if a supplier with the same name already exists.
    """
    if supplier_data.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create a supplier for a different organisation",
        )
    return await PurchaseOrderService.create_supplier(db, supplier_data, current_user)


@supplier_router.get(
    "/{supplier_id}",
    response_model=SupplierResponse,
    summary="Get supplier",
)
async def get_supplier(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Supplier:
    """Fetch a supplier by ID."""
    supplier = await PurchaseOrderService.get_supplier(db, supplier_id)
    if supplier.organization_id != current_user.organization_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return supplier


@supplier_router.patch(
    "/{supplier_id}",
    response_model=SupplierResponse,
    dependencies=[Depends(require_permission("manage_suppliers"))],
    summary="Update supplier",
)
async def update_supplier(
    supplier_id: uuid.UUID,
    update_data: SupplierUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Supplier:
    """
    Partially update a supplier.

    **Permissions:** `manage_suppliers`
    """
    supplier = await PurchaseOrderService.get_supplier(db, supplier_id)
    if supplier.organization_id != current_user.organization_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    for field, value in update_data.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)

    supplier.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(supplier)
    return supplier


@supplier_router.get(
    "/",
    response_model=PaginatedResponse[SupplierResponse],
    summary="List suppliers",
)
async def list_suppliers(
    pagination: PaginationParams = Depends(PaginationParams),
    active_only: Annotated[bool, Query(description="Return only active suppliers")] = True,
    search: Annotated[Optional[str], Query(description="Search by name or contact person")] = None,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
) -> PaginatedResponse[SupplierResponse]:
    """
    List suppliers with pagination, optional search, and active-only filter.

    **Query Parameters:**
    - `page` / `page_size` — pagination
    - `active_only` — filter to active suppliers (default: `true`)
    - `search` — case-insensitive match on name or contact_person
    """
    query = select(Supplier).where(
        Supplier.organization_id == organization_id,
        Supplier.is_deleted.is_(False),
    )

    if active_only:
        query = query.where(Supplier.is_active.is_(True))

    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                Supplier.name.ilike(pattern),
                Supplier.contact_person.ilike(pattern),
            )
        )

    query = query.order_by(Supplier.name.asc(), Supplier.id.asc())

    paginator = Paginator(db)
    return await paginator.paginate(query, params=pagination, schema=SupplierResponse)


# =============================================================================
# Purchase Order Endpoints
# =============================================================================

@router.post(
    "/",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("manage_inventory"))],
    summary="Create purchase order",
)
async def create_purchase_order(
    po_data: PurchaseOrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrder:
    """
    Create a new purchase order in **draft** status.

    **Permissions:** `manage_inventory`

    **Workflow:** draft → pending → approved → (ordered) → received

    ```json
    {
      "branch_id": "uuid",
      "supplier_id": "uuid",
      "items": [
        { "drug_id": "uuid", "quantity_ordered": 100, "unit_cost": 5.50 }
      ],
      "expected_delivery_date": "2026-06-01",
      "shipping_cost": 25.00,
      "notes": "Urgent restock"
    }
    ```
    """
    return await PurchaseOrderService.create_purchase_order(db, po_data, current_user)


@router.get(
    "/",
    response_model=PaginatedResponse[PurchaseOrderResponse],
    summary="List purchase orders",
)
async def list_purchase_orders(
    pagination: PaginationParams = Depends(PaginationParams),
    branch_id: Annotated[Optional[uuid.UUID], Query(description="Filter by branch")] = None,
    status_filter: Annotated[
        Optional[str],
        Query(
            alias="status",
            description="Filter by status: draft | pending | approved | ordered | received | cancelled",
            pattern="^(draft|pending|approved|ordered|received|cancelled)$",
        ),
    ] = None,
    supplier_id: Annotated[Optional[uuid.UUID], Query(description="Filter by supplier")] = None,
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id),
) -> PaginatedResponse[PurchaseOrderResponse]:
    """
    List purchase orders with pagination and optional filters.
    """
    query = select(PurchaseOrder).where(
        PurchaseOrder.organization_id == organization_id
    )

    if branch_id:
        query = query.where(PurchaseOrder.branch_id == branch_id)
    if status_filter:
        query = query.where(PurchaseOrder.status == status_filter)
    if supplier_id:
        query = query.where(PurchaseOrder.supplier_id == supplier_id)

    query = query.order_by(PurchaseOrder.created_at.desc())

    paginator = Paginator(db)
    return await paginator.paginate(query, params=pagination, schema=PurchaseOrderResponse)


@router.get(
    "/{po_id}",
    response_model=PurchaseOrderWithDetails,
    summary="Get purchase order",
)
async def get_purchase_order(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrderWithDetails:
    """Fetch a purchase order with full item and supplier details."""
    po = await PurchaseOrderService.get_purchase_order(db, po_id, include_details=True)

    if po.organization_id != current_user.organization_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return await PurchaseOrderService._build_po_with_details(db, po)


# =============================================================================
# Workflow Transition Endpoints
# =============================================================================

@router.post(
    "/{po_id}/submit",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_permission("manage_inventory"))],
    summary="Submit PO for approval",
)
async def submit_for_approval(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrder:
    """
    Transition a draft PO to **pending** (awaiting approval).

    **Permissions:** `manage_inventory`
    """
    return await PurchaseOrderService.submit_for_approval(db, po_id, current_user)


@router.post(
    "/{po_id}/approve",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_permission("approve_purchase_orders"))],
    summary="Approve purchase order",
)
async def approve_purchase_order(
    po_id: uuid.UUID,
    approval_data: PurchaseOrderApprove,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrder:
    """
    Approve a pending PO — transitions to **approved**.

    **Permissions:** `approve_purchase_orders` (admin / super_admin)

    Approvers cannot approve their own orders (four-eyes principle).
    """
    return await PurchaseOrderService.approve_purchase_order(db, po_id, current_user)


@router.post(
    "/{po_id}/reject",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_permission("approve_purchase_orders"))],
    summary="Reject purchase order",
)
async def reject_purchase_order(
    po_id: uuid.UUID,
    rejection_data: PurchaseOrderReject,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrder:
    """
    Reject a pending PO — transitions to **cancelled**.

    **Permissions:** `approve_purchase_orders`

    The rejection reason is prepended to the PO notes field for traceability.
    """
    return await PurchaseOrderService.reject_purchase_order(
        db, po_id, rejection_data.reason, current_user
    )


@router.post(
    "/{po_id}/cancel",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_permission("manage_inventory"))],
    summary="Cancel purchase order",
)
async def cancel_purchase_order(
    po_id: uuid.UUID,
    cancel_data: PurchaseOrderCancel,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrder:
    """
    Cancel a **draft** or **pending** PO.

    Approved, ordered, or received POs cannot be cancelled through this endpoint.

    **Permissions:** `manage_inventory`
    """
    return await PurchaseOrderService.cancel_purchase_order(
        db, po_id, cancel_data.reason, current_user
    )


@router.post(
    "/{po_id}/receive",
    response_model=ReceivePurchaseOrderResponse,
    dependencies=[Depends(require_permission("manage_inventory"))],
    summary="Receive goods",
)
async def receive_goods(
    po_id: uuid.UUID,
    receive_data: ReceivePurchaseOrder,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReceivePurchaseOrderResponse:
    """
    Record receipt of goods against an approved (or partially received) PO.

    **Permissions:** `manage_inventory`

    **Effects per item:**
    - Creates a `DrugBatch` for FEFO expiry tracking
    - Updates `BranchInventory` (with row-level lock to prevent races)
    - Creates an immutable `StockAdjustment` audit row
    - Recalculates `Drug.cost_price` using weighted-average costing

    When all items are fully received the PO status transitions to `received`.
    Partial receipt sets the status to `ordered`.

    ```json
    {
      "received_date": "2026-04-01",
      "items": [
        {
          "purchase_order_item_id": "uuid",
          "quantity_received": 100,
          "batch_number": "BATCH-2026-001",
          "manufacturing_date": "2025-12-01",
          "expiry_date": "2028-12-01"
        }
      ],
      "notes": "All items in good condition"
    }
    ```
    """
    return await PurchaseOrderService.receive_goods(db, po_id, receive_data, current_user)


# =============================================================================
# PO Item Endpoints
# =============================================================================

@router.post(
    "/{po_id}/items",
    response_model=PurchaseOrderWithDetails,
    dependencies=[Depends(require_permission("manage_inventory"))],
    status_code=status.HTTP_201_CREATED,
    summary="Add items to draft PO",
)
async def add_purchase_order_items(
    po_id: uuid.UUID,
    items: List[PurchaseOrderItemCreate],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrderWithDetails:
    """
    Append one or more drug lines to a **draft** purchase order.

    **Permissions:** `manage_inventory`

    - PO must be in `draft` status
    - Drugs already present in the PO will cause a **409** — update them instead
    - All `drug_id` values must belong to your organisation

    ```json
    [
      { "drug_id": "uuid", "quantity_ordered": 50, "unit_cost": 12.50 },
      { "drug_id": "uuid", "quantity_ordered": 100, "unit_cost": 8.75 }
    ]
    ```
    """
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one item",
        )

    po = await PurchaseOrderService.add_purchase_order_items(
        db, po_id, items, current_user
    )
    return await PurchaseOrderService._build_po_with_details(db, po)


@router.patch(
    "/{po_id}/items/{item_id}",
    response_model=PurchaseOrderWithDetails,
    dependencies=[Depends(require_permission("manage_inventory"))],
    summary="Update a PO line item",
)
async def update_purchase_order_item(
    po_id: uuid.UUID,
    item_id: uuid.UUID,
    quantity_ordered: Annotated[int, Query(gt=0, description="New quantity (must be > 0)")],
    unit_cost: Annotated[Decimal, Query(gt=0, description="New unit cost (must be > 0)")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PurchaseOrderWithDetails:
    """
    Update the quantity and/or unit cost of an item in a **draft** PO.

    **Permissions:** `manage_inventory`

    ```
    PATCH /purchase-orders/{po_id}/items/{item_id}?quantity_ordered=75&unit_cost=11.25
    ```
    """
    po = await PurchaseOrderService.update_purchase_order_item(
        db, po_id, item_id, quantity_ordered, unit_cost, current_user
    )
    return await PurchaseOrderService._build_po_with_details(db, po)


@router.get(
    "/{po_id}/items",
    response_model=List[PurchaseOrderItemWithDetails],
    summary="List PO items",
)
async def list_purchase_order_items(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PurchaseOrderItemWithDetails]:
    """
    Return all line items for a purchase order, including resolved drug details.
    """
    return await PurchaseOrderService.list_purchase_order_items(db, po_id, current_user)