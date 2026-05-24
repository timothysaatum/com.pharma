"""
Sales API Routes
FastAPI endpoints for sales transactions, refunds, and reporting
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from datetime import datetime, timezone
import uuid

from app.core.deps import get_current_user, get_organization_id, require_permission
from app.db.dependencies import get_db
from app.models.user.user_model import User
from app.schemas.sales_schemas import (
    SaleResponse, SaleWithDetails,
    SaleCreate, ProcessSaleResponse,
    RefundSaleRequest, RefundSaleResponse,
    CancelSaleRequest, ReceiptData,
    SaleFilters
)
from app.services.sales.sales_service import SalesService
from app.services.sales.utils.sale_helpers import build_sale_with_details
from app.utils.pagination import PaginatedResponse, Paginator, PaginationParams

router = APIRouter(prefix="/sales", tags=["Sales"])


# ============================================
# Sale Processing
# ============================================

@router.post(
    "/",
    response_model=ProcessSaleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("process_sales"))]
)
async def process_sale(
    sale_data: SaleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Process a customer sale
    
    **Permissions:** process_sales
    
    **Critical Operation:**
    - Validates price contract eligibility and applies contract pricing
    - Validates inventory availability
    - Validates prescription if required
    - Updates inventory using FEFO (First Expire, First Out)
    - Updates drug batches
    - Creates stock adjustment audit trail
    - Awards loyalty points
    - Creates low stock alerts if needed
    
    **Request Body:**
    ```json
    {
      "branch_id": "uuid",
      "price_contract_id": "uuid",
      "customer_id": "uuid",
      "customer_name": "Walk-in Customer",
      "items": [
        {
          "drug_id": "uuid",
          "quantity": 2,
          "requires_prescription": false,
          "prescription_verified": false
        }
      ],
      "payment_method": "cash",
      "amount_paid": 30.00,
      "insurance_verified": false,
      "insurance_claim_number": null,
      "prescription_id": null,
      "notes": "Customer request"
    }
    ```
    
    **Response:**
    Returns sale details with:
    - Inventory updates count
    - Batches updated count (FEFO)
    - Loyalty points awarded
    - Low stock alerts created
    - Contract applied and discount given
    """
    return await SalesService.process_sale(db, sale_data, current_user)


@router.get(
    "/{sale_id}",
    response_model=SaleWithDetails
)
async def get_sale(
    sale_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get sale by ID with full details
    
    Returns sale with:
    - All sale items with drug details
    - Customer information
    - Cashier information
    - Branch information
    - Loyalty points earned
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.sales.sales_model import Sale
    
    result = await db.execute(
        select(Sale)
        .options(selectinload(Sale.items))
        .where(Sale.id == sale_id)
    )
    sale = result.scalar_one_or_none()
    
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found"
        )
    
    # Check organization access
    if sale.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    return await build_sale_with_details(db, sale)


@router.get(
    "/",
    response_model=PaginatedResponse[SaleResponse]
)
async def list_sales(
    pagination: PaginationParams = Depends(),
    branch_id: Optional[uuid.UUID] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    sale_status: Optional[str] = Query(None, alias="status", pattern="^(draft|completed|cancelled|refunded)$"),
    payment_status: Optional[str] = Query(None, pattern="^(pending|completed|partial|refunded|cancelled)$"),
    payment_method: Optional[str] = Query(None),
    customer_id: Optional[uuid.UUID] = Query(None),
    cashier_id: Optional[uuid.UUID] = Query(None),
    price_contract_id: Optional[uuid.UUID] = Query(None, description="Filter by price contract"),
    contract_type: Optional[str] = Query(None, pattern="^(insurance|corporate|staff|senior_citizen|standard|wholesale|promotional)$"),
    db: AsyncSession = Depends(get_db),
    organization_id: uuid.UUID = Depends(get_organization_id)
):
    """
    List sales with pagination and filtering
    
    **Query Parameters:**
    - page: Page number (default: 1)
    - page_size: Items per page (default: 50)
    - branch_id: Filter by branch
    - start_date: Filter sales from this date
    - end_date: Filter sales until this date
    - status: Filter by status (draft, completed, cancelled, refunded)
    - payment_status: Filter by payment status
    - payment_method: Filter by payment method (cash, card, mobile_money, insurance, credit, split)
    - customer_id: Filter by customer
    - cashier_id: Filter by cashier
    - price_contract_id: Filter by price contract applied
    - contract_type: Filter by contract type (insurance, corporate, staff, etc.)
    
    **Example:**
    ```
    GET /sales?branch_id=xxx&start_date=2026-02-01T00:00:00Z&status=completed&contract_type=insurance
    ```
    """
    from sqlalchemy import select, and_
    from app.models.sales.sales_model import Sale
    
    filters = [Sale.organization_id == organization_id]
    
    if branch_id:
        filters.append(Sale.branch_id == branch_id)
    
    if start_date:
        filters.append(Sale.created_at >= start_date)
    
    if end_date:
        filters.append(Sale.created_at <= end_date)
    
    if sale_status:
        filters.append(Sale.status == sale_status)
    
    if payment_status:
        filters.append(Sale.payment_status == payment_status)
    
    if payment_method:
        filters.append(Sale.payment_method == payment_method)
    
    if customer_id:
        filters.append(Sale.customer_id == customer_id)
    
    if cashier_id:
        filters.append(Sale.cashier_id == cashier_id)
    
    if price_contract_id:
        filters.append(Sale.price_contract_id == price_contract_id)
    
    from app.models.pricing.pricing_model import PriceContract

    query = (
        select(Sale)
        .where(and_(*filters))
        .order_by(Sale.created_at.desc())
    )

    if contract_type:
        # Join PriceContract to filter by contract_type column (ORM relationship
        # attributes cannot be used as SQL expressions directly).
        query = (
            query
            .join(PriceContract, Sale.price_contract_id == PriceContract.id)
            .where(PriceContract.contract_type == contract_type)
        )
    
    paginator = Paginator(db)
    return await paginator.paginate(query, pagination, SaleResponse)


# ============================================
# Sale Actions
# ============================================

@router.post(
    "/{sale_id}/refund",
    response_model=RefundSaleResponse,
    dependencies=[Depends(require_permission("process_refunds"))]
)
async def refund_sale(
    sale_id: uuid.UUID,
    refund_data: RefundSaleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Refund a sale (full or partial)
    
    **Permissions:** process_refunds (manager, admin, super_admin)
    
    **Critical Operation:**
    - Restores inventory for refunded items
    - Creates stock adjustment audit trail
    - Deducts loyalty points
    - Updates sale status to 'refunded'
    
    **Request Body:**
    ```json
    {
      "reason": "Customer returned unopened items",
      "refund_amount": 30.00,
      "items_to_refund": [
        {
          "sale_item_id": "uuid",
          "quantity": 2
        }
      ]
    }
    ```
    """
    return await SalesService.refund_sale(db, sale_id, refund_data, current_user)


@router.post(
    "/{sale_id}/cancel",
    response_model=SaleResponse,
    dependencies=[Depends(require_permission("process_sales"))]
)
async def cancel_sale(
    sale_id: uuid.UUID,
    cancel_data: CancelSaleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Cancel a sale (draft status only)
    
    **Permissions:** process_sales
    
    Only sales in 'draft' status can be cancelled.
    For completed sales, use the refund endpoint.
    Cancellation requires a manager approval user ID and a detailed reason.
    Inventory can optionally be restored on cancellation.
    """
    from sqlalchemy import select
    from app.models.sales.sales_model import Sale
    from app.models.user.user_model import User as UserModel
    
    result = await db.execute(
        select(Sale)
        .where(Sale.id == sale_id)
        .with_for_update()
    )
    sale = result.scalar_one_or_none()
    
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found"
        )
    
    if sale.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    if sale.status != 'draft':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel sale with status '{sale.status}'. Use refund endpoint for completed sales."
        )
    
    # Validate manager approval
    if cancel_data.manager_approval_user_id != current_user.id:
        result = await db.execute(
            select(UserModel).where(
                UserModel.id == cancel_data.manager_approval_user_id,
                UserModel.organization_id == current_user.organization_id
            )
        )
        approver = result.scalar_one_or_none()
        if not approver:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approving manager not found"
            )
        if approver.role not in ['manager', 'admin', 'super_admin']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cancellation approver must be a manager or admin"
            )
    
    sale.status = 'cancelled'
    sale.cancelled_at = datetime.now(timezone.utc)
    sale.cancelled_by = current_user.id
    sale.cancellation_reason = cancel_data.reason
    sale.mark_as_pending_sync()
    
    await db.commit()
    await db.refresh(sale)
    
    return sale


# ============================================
# Receipt Generation
# ============================================

@router.get(
    "/{sale_id}/receipt",
    response_model=dict
)
async def get_receipt(
    sale_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get receipt data for a sale
    
    Returns formatted receipt data suitable for printing or emailing.
    Includes full contract and insurance breakdown.
    """
    sale_details = await get_sale(sale_id, db, current_user)
    
    from sqlalchemy import select
    from app.models.pharmacy.pharmacy_model import Organization, Branch
    
    result = await db.execute(
        select(Organization).where(Organization.id == sale_details.organization_id)
    )
    organization = result.scalar_one()
    
    result = await db.execute(
        select(Branch).where(Branch.id == sale_details.branch_id)
    )
    branch = result.scalar_one()
    
    receipt = {
        "receipt_number": sale_details.sale_number,
        "receipt_date": sale_details.created_at,
        "organization": {
            "name": organization.name,
            "tax_id": getattr(organization, 'tax_id', None),
            "phone": getattr(organization, 'phone', None),
            "email": getattr(organization, 'email', None),
        },
        "branch": {
            "name": branch.name,
            "address": getattr(branch, 'address', None),
            "phone": getattr(branch, 'phone', None),
        },
        "customer": {
            "name": sale_details.customer_full_name or sale_details.customer_name or "Walk-in Customer",
            "phone": sale_details.customer_phone,
        },
        "items": [
            {
                "name": item.drug_name,
                "generic_name": item.drug_generic_name,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "subtotal": float(item.subtotal),
                "contract_discount": float(item.contract_discount_amount),
                "additional_discount": float(item.additional_discount_amount),
                "total_discount": float(item.total_discount_amount),
                "tax": float(item.tax_amount),
                "total": float(item.total_price),
                "batch_number": item.batch_number,
                "insurance_covered": item.insurance_covered,
                "patient_copay": float(item.patient_copay) if item.patient_copay else None,
            }
            for item in sale_details.items
        ],
        "subtotal": float(sale_details.subtotal),
        "contract_discount": float(sale_details.contract_discount_amount),
        "additional_discount": float(sale_details.additional_discount_amount),
        "total_discount": float(sale_details.total_discount_amount),
        "tax": float(sale_details.tax_amount),
        "total": float(sale_details.total_amount),
        "amount_paid": float(sale_details.amount_paid),
        "change": float(sale_details.change_amount),
        "payment_method": sale_details.payment_method,
        "cashier": sale_details.cashier_name,
        # Contract details
        "contract": {
            "name": sale_details.contract_name,
            "type": sale_details.contract_type,
            "discount_percentage": float(sale_details.contract_discount_percentage or 0),
        } if sale_details.contract_name else None,
        # Insurance details
        "insurance": {
            "claim_number": sale_details.insurance_claim_number,
            "preauth_number": sale_details.insurance_preauth_number,
            "patient_copay": float(sale_details.patient_copay_amount or 0),
            "insurance_covered": float(sale_details.insurance_covered_amount or 0),
            "verified": sale_details.insurance_verified,
        } if sale_details.insurance_claim_number else None,
    }
    
    return receipt